"""hwp_glue — target-specific JS wrappers for Stage 76.

``hlwasm`` already emits the compact runtime glue (module
instantiation, ``Halis.run``, struct marshalling, ``callHalis``).
This module wraps that glue per publish target so the packed
``pkg/*.js`` is directly consumable:

  bundler    ESM ``import init from './name.js'`` (webpack/vite).
  web        ESM bare — ``new URL('./name.wasm', import.meta.url)``.
  nodejs     CommonJS ``const { init } = require('./name.js')``.
  deno       ESM + ``deno.json`` hint (``deno run --allow-read``).
  no-modules IIFE assigning the global ``HalisPack`` (``<script>``).

Each wrapper embeds the hlwasm compact glue verbatim (no fork: the
runtime stays single-sourced) and adds a thin async ``init`` +
re-export layer on top. A ``__TARGET__`` marker comment names the
target so ``check`` / acceptance can verify the flavour.
"""
from __future__ import annotations

from typing import Optional

_MARKER = "/* hls-wasm-pack target: %s (Stage 76) */"


def _bundler_js(stem: str, glue: str) -> str:
    return "\n".join([
        _MARKER % "bundler",
        "/* ESM bundler entry — import init from './%s.js' */" % stem,
        glue,
        "",
        "const __wasmUrl = new URL('./%s.wasm', import.meta.url);" % stem,
        "export async function init(input) {",
        "  const src = input || __wasmUrl;",
        "  const bytes = src instanceof ArrayBuffer ? new Uint8Array(src) : await (await fetch(src)).arrayBuffer().then(b => new Uint8Array(b));",
        "  return Halis.run(bytes);",
        "}",
        "export default init;",
        "export const HalisPack = (typeof Halis !== 'undefined') ? Halis : null;",
        "",
    ])


def _web_js(stem: str, glue: str) -> str:
    return "\n".join([
        _MARKER % "web",
        "/* ESM bare entry — <script type=\"module\">, no bundler. */",
        glue,
        "",
        "export async function init(input) {",
        "  const url = input || new URL('./%s.wasm', import.meta.url);" % stem,
        "  const resp = await fetch(url);",
        "  const bytes = new Uint8Array(await resp.arrayBuffer());",
        "  return Halis.run(bytes);",
        "}",
        "export default init;",
        "",
    ])


def _nodejs_js(stem: str, glue: str) -> str:
    # The compact glue is browser-flavoured ESM; for Node we keep it
    # verbatim (single-sourced runtime) and add a CJS loader around it.
    cjs_loader = "\n".join([
        "const fs = require('fs');",
        "const path = require('path');",
        "async function init(input) {",
        "  const wasmPath = input || path.join(__dirname, '%s.wasm');" % stem,
        "  const bytes = new Uint8Array(fs.readFileSync(wasmPath));",
        "  return Halis.run(bytes);",
        "}",
        "module.exports = { init, default: init, HalisPack: (typeof Halis !== 'undefined') ? Halis : null };",
    ])
    return "\n".join([_MARKER % "nodejs", glue, "", cjs_loader, ""])


def _deno_js(stem: str, glue: str) -> str:
    return "\n".join([
        _MARKER % "deno",
        "/* Deno entry — deno run --allow-read mod.ts */",
        glue,
        "",
        "export async function init(input) {",
        "  const url = input || new URL('./%s.wasm', import.meta.url);" % stem,
        "  const bytes = new Uint8Array(await Deno.readFile(url));",
        "  return Halis.run(bytes);",
        "}",
        "export default init;",
        "",
    ])


def _no_modules_js(stem: str, glue: str) -> str:
    return "\n".join([
        _MARKER % "no-modules",
        "/* IIFE global entry — <script src=\"./%s.js\"></script> gives window.HalisPack. */" % stem,
        "(function (global, factory) {",
        "  const api = factory();",
        "  global.HalisPack = api;",
        "}(typeof self !== 'undefined' ? self : this, function () {",
        glue,
        "  async function init(input) {",
        "    const url = input || './%s.wasm';" % stem,
        "    const resp = await fetch(url);",
        "    const bytes = new Uint8Array(await resp.arrayBuffer());",
        "    return Halis.run(bytes);",
        "  }",
        "  return { init: init, Halis: (typeof Halis !== 'undefined') ? Halis : null };",
        "}));",
        "",
    ])


def render_js(target: str, stem: str, glue: str) -> str:
    """Render the target-specific ``.js`` for ``stem`` + compact ``glue``."""
    builders = {
        "bundler": _bundler_js,
        "web": _web_js,
        "nodejs": _nodejs_js,
        "deno": _deno_js,
        "no-modules": _no_modules_js,
    }
    if target not in builders:
        raise ValueError(
            "unknown pack target %r (expected one of: %s)"
            % (target, ", ".join(sorted(builders))))
    return builders[target](stem, glue.rstrip() + "\n")


def deno_json(package_name: str) -> str:
    """Return the ``deno.json`` hint content for the deno target."""
    import json as _json
    return _json.dumps({"name": package_name, "exports": "./mod.js",
                        "tasks": {"start": "deno run --allow-read mod.js"}},
                       indent=2) + "\n"


__all__ = [
    "deno_json",
    "render_js",
]
