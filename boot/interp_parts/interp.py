"""The Interp class assembled from its mixins.

Verbatim reassembly of the original boot/interp.py class Interp
(lines 691..EOF). Every method name exists exactly once across the
mixins, so method resolution is identical."""
from .core import InterpCore
from .exec import InterpExec
from .builtin import InterpBuiltin
from .spawn import InterpSpawn
from .extern import InterpExtern
from .builtin_method import InterpBuiltin_method


class Interp(InterpCore, InterpExec, InterpBuiltin, InterpSpawn, InterpExtern, InterpBuiltin_method):
    """Stage-0 evaluator (unchanged API)."""
