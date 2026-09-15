"""LLVM IR emitter (facade).

Implementation split into the tools/llvm_parts/ package;
this module re-exports the original public API."""
from __future__ import annotations
import os as _os
import sys as _sys
_PKG_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "llvm_parts")
if _PKG_DIR not in _sys.path:
    _sys.path.insert(0, _PKG_DIR)
from llvm_common import *  # noqa: F401,F403
from llvm_emit_core import *  # noqa: F401,F403
from llvm_emit_lower import *  # noqa: F401,F403
from llvm_emit_ops import *  # noqa: F401,F403
from llvm_emit_calls import *  # noqa: F401,F403
from llvm_llvmemitter import LLVMEmitter  # noqa: F401


def emit_module(program, target_triple: Optional[str] = None) -> str:
    """Convenience: emit LLVM IR for a checked program."""
    return LLVMEmitter(program, target_triple).emit()
