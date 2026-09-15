"""hlwasm - HLS -> WebAssembly compiler (facade).

Implementation split into the tools/hlwasm_parts/ package;
this module re-exports the original public API."""
from __future__ import annotations
import os as _os
import sys as _sys
_PKG_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "hlwasm_parts")
if _PKG_DIR not in _sys.path:
    _sys.path.insert(0, _PKG_DIR)
from hwasm_common import *  # noqa: F401,F403
from hwasm_module import *  # noqa: F401,F403
from hwasm_emit_core import *  # noqa: F401,F403
from hwasm_emit_runtime import *  # noqa: F401,F403
from hwasm_emit_lower import *  # noqa: F401,F403
from hwasm_glue_cli import *  # noqa: F401,F403
from hwasm_wasmemitter import WasmEmitter  # noqa: F401


if __name__ == "__main__":
    sys.exit(main())
