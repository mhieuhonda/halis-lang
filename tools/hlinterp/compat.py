"""Python version compatibility helpers for the Stage-0 boot package.

The project supports Python 3.8+ (README: "Python 3.8+ (only for the
Stage-0 seed)"; the CI matrix runs 3.8 and 3.11). commit d80af56 added
`strict=True` to every zip() call (flake8-bugbear B905), but that
keyword only exists on Python 3.10+ — on 3.8/3.9 `zip()` takes no
keyword arguments at all, so EVERY program crashed with
`TypeError: zip() takes no keyword arguments` (the root cause of the
CI matrix legs running Python 3.8 failing since 2026-09-09).

`zip_strict` provides the exact `zip(strict=True)` semantics on every
supported version: it yields tuples while ALL iterables have items and
raises ValueError as soon as one iterable is exhausted while another
still has items left. On Python 3.10+ it delegates to the builtin so
behavior (including error messages) is identical there.
"""
import sys

__all__ = ["zip_strict"]

_EMPTY = object()


def _zip_strict_py(*iterables):
    """Pure-Python zip(strict=True) for Python < 3.10.

    Also used by the test-suite to pin the semantics independently of
    the running interpreter version.
    """
    if not iterables:
        return
    iters = [iter(it) for it in iterables]
    while True:
        vals = []
        alive = False
        dead = False
        for it in iters:
            v = next(it, _EMPTY)
            if v is _EMPTY:
                dead = True
            else:
                alive = True
                vals.append(v)
        if dead and alive:
            raise ValueError(
                "zip(): iterators have different lengths "
                "(strict mode requires equal lengths)")
        if dead:
            return
        yield tuple(vals)


if sys.version_info >= (3, 10):

    def zip_strict(*iterables):
        """Exact alias for builtin zip(*iterables, strict=True)."""
        return zip(*iterables, strict=True)

else:

    def zip_strict(*iterables):
        """Exact alias for builtin zip(*iterables, strict=True)."""
        return _zip_strict_py(*iterables)
