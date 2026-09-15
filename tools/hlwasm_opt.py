"""hlwasm_opt - wasm optimizer (facade).

Implementation split into the tools/wopt_parts/ package;
this module re-exports the original public API."""
from __future__ import annotations
import os as _os
import sys as _sys
_PKG_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "wopt_parts")
if _PKG_DIR not in _sys.path:
    _sys.path.insert(0, _PKG_DIR)
from wopt_common import *  # noqa: F401,F403
from wopt_module import *  # noqa: F401,F403
from wopt_analysis import *  # noqa: F401,F403
from wopt_passes import *  # noqa: F401,F403
from wopt_emit_cli import *  # noqa: F401,F403


if __name__ == "__main__":
    sys.exit(main())
