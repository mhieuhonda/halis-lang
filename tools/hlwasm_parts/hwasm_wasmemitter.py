"""WasmEmitter - assembled from its mixins (verbatim segments)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hwasm_emit_core import WasmEmitterCore
from hwasm_emit_runtime import WasmEmitterRuntime
from hwasm_emit_lower import WasmEmitterLower


class WasmEmitter(WasmEmitterCore, WasmEmitterRuntime, WasmEmitterLower):
    """WasmEmitter (unchanged API/behavior)."""
