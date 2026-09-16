"""zip(strict=True) compatibility for the tools packages.

zip(strict=True) requires Python 3.10+, but the project supports
Python 3.8+ (see boot/compat.py for the full rationale). This module
re-exports the same exact-semantics helper implemented in boot/compat
as a standalone copy (the tools packages are plain sys.path modules,
not part of the boot package, so they cannot relative-import it).
"""
_EMPTY = object()


def _zip_strict_py(*iterables):
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


try:
    _zip_strict_builtin = zip(*(), strict=True)  # probe: 3.10+
    def zip_strict(*iterables):
        return zip(*iterables, strict=True)
except TypeError:
    def zip_strict(*iterables):
        return _zip_strict_py(*iterables)
