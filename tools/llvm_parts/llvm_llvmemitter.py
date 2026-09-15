"""LLVMEmitter - assembled from its mixins (verbatim segments)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from llvm_emit_core import LLVMEmitterCore
from llvm_emit_lower import LLVMEmitterLower
from llvm_emit_ops import LLVMEmitterOps
from llvm_emit_calls import LLVMEmitterCalls


class LLVMEmitter(LLVMEmitterCore, LLVMEmitterLower, LLVMEmitterOps, LLVMEmitterCalls):
    """LLVMEmitter (unchanged API/behavior)."""
