"""hls-pkg - package manager CLI (facade).

Implementation split into the tools/hpkg_parts/ package;
this module re-exports the original public API."""
from __future__ import annotations
import os as _os
import sys as _sys
_PKG_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "hpkg_parts")
if _PKG_DIR not in _sys.path:
    _sys.path.insert(0, _PKG_DIR)
from hpkg_common import *  # noqa: F401,F403
from hpkg_log import *  # noqa: F401,F403
from hpkg_manifest import *  # noqa: F401,F403
from hpkg_util import *  # noqa: F401,F403
from hpkg_deps import *  # noqa: F401,F403
from hpkg_cmds import *  # noqa: F401,F403
from hpkg_cli import *  # noqa: F401,F403


if __name__ == "__main__":
    sys.exit(main())
