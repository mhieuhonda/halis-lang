"""The Checker class assembled from its mixins + the check() entry.

Verbatim reassembly of the original boot/checker.py (lines 698..end).
Every method name exists exactly once across the mixins, so method
resolution is identical."""
from .core import CheckerCore
from .stmt import CheckerStmt
from .expr import CheckerExpr
from .call import CheckerCall
from .match_fx import CheckerMatch_fx
from .escape import CheckerEscape
from .tail import CheckerTail
from .contract import CheckerContract


class Checker(CheckerCore, CheckerStmt, CheckerExpr, CheckerCall, CheckerMatch_fx, CheckerEscape, CheckerTail, CheckerContract):
    """Stage-0 type checker & effects analyzer (unchanged API)."""


def check(program):
    """Type-check + effects-check a program. Returns the Checker instance
    so callers (e.g. boot.py --audit) can inspect program['computed_effects']
    and the per-function declared effects."""
    c = Checker(program)
    c.check()
    return c
