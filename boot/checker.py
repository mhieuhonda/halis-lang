"""Stage-0 type checker & effects analyzer for HLS.

Facade: the implementation lives in the boot/checking/ package
(split for maintainability). All public names are re-exported so
that `from boot.checker import check` (and every helper) keeps
working."""
from .checking.helpers import *  # noqa: F401,F403
from .checking.helpers import HLError  # noqa: F401
from .checking.checker import Checker, check  # noqa: F401
