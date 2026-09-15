"""Stage-0 evaluator for HLS.

Facade: the implementation lives in the boot/interp_parts/ package
(split for maintainability). Interp and every runtime name are
re-exported so `from boot.interp import Interp, HLPanic, ...` and
`from boot.interp import _cpu_supports` keep working."""
from .interp_parts.rt import *  # noqa: F401,F403
from .interp_parts.interp import Interp  # noqa: F401
