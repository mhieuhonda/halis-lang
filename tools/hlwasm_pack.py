#!/usr/bin/env python3
"""hlwasm_pack.py — Stage 76 (v0.95.0-alpha): ``hls-wasm-pack``.

The ``wasm-pack`` equivalent for Halis web apps. Turns a ``.hls``
program into a publish-ready npm package (``pkg/``): ``.wasm`` +
target-specific ``.js`` glue + ``.d.ts`` + ``package.json`` +
``README.md`` + ``.pack-manifest.json`` — then optionally packs it
into a ``.tgz`` and (dry-run by default) publishes it to an npm
registry.

This is the public entrypoint. Implementation split into the
``tools/hlwasm_pack_parts/`` package (mirrors the Stage 75
``tools/hlserve_parts/`` pattern). This module re-exports ``main``
so existing tooling (``make wasm-pack*``, ``tests/``) keeps working.

Subcommands: ``new`` / ``build`` / ``pack`` / ``publish`` /
``check`` / ``test``. Targets: ``bundler`` / ``web`` / ``nodejs`` /
``deno`` / ``no-modules``. No new compiler builtins; this is a pure
tooling stage on top of the Stage 23/24 ``hlwasm`` backend, the
Stage 24 ``hlwasm_opt`` size optimizer, and the Stage 73
``std.jsffi`` extern surface.
"""
from __future__ import annotations

import os
import sys

# Re-export the modular implementation.
_PKG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "hlwasm_pack_parts")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from hwp_cli import main  # noqa: F401,E402


if __name__ == "__main__":
    sys.exit(main())
