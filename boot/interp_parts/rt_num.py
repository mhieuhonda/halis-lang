"""Interp runtime support (rt_num) - verbatim segment of the
original boot/interp.py (lines 404..591), split for maintainability."""
from .rt_core import (
    HLPanic, INT64_MAX, INT64_MIN, math, platform,
)

# ---------- int64 checked arithmetic ----------
def i64_add(a, b, line):
    r = a + b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_sub(a, b, line):
    r = a - b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_mul(a, b, line):
    r = a * b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_neg(a, line):
    if a == INT64_MIN:
        raise HLPanic("integer overflow", line)
    return -a


def i64_div(a, b, line):
    if b == 0:
        raise HLPanic("division by zero", line)
    if a == INT64_MIN and b == -1:
        raise HLPanic("integer overflow", line)
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def i64_mod(a, b, line):
    if b == 0:
        raise HLPanic("division by zero", line)
    if a == INT64_MIN and b == -1:
        raise HLPanic("integer overflow", line)
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def f64_div(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        # BUG-SC-6 fix: handle -0.0 divisor correctly. Previously the
        # sign test `(a > 0) == (b >= 0)` treated +0.0 and -0.0 the same
        # (both pass `>= 0`), producing the wrong sign of infinity when
        # the divisor was -0.0. Use math.copysign to distinguish them.
        # Deep-scan-25 fix (interpreter/native parity): an x86-64 divide
        # with a NaN numerator PROPAGATES the input NaN (quieted) rather
        # than synthesising the default QNaN, and 0.0/0.0 raises the
        # hardware default QNaN (sign bit SET on x86-64). Mirror both.
        if a != a:
            return a
        if a == 0:
            return _NAN_DEFAULT
        # copysign(1.0, x) returns 1.0 for +x (incl. +0.0) and -1.0 for -x
        # (incl. -0.0). Result is +inf iff signs of a and b agree.
        same_sign = math.copysign(1.0, a) == math.copysign(1.0, b)
        return float("inf") if same_sign else float("-inf")


def f64_mod(a, b):
    try:
        return math.fmod(a, b)
    except ValueError:
        # Deep-scan-25 fix (interpreter/native parity): C fmod(x, 0) is a
        # domain error that yields the hardware default QNaN (sign bit
        # set on x86-64); a NaN numerator propagates unchanged.
        if a != a:
            return a
        return _NAN_DEFAULT


def parse_int(s, line):
    """Convert bytes -> int, matching the C semantics of builtin int()/to_int()."""
    n = len(s)
    i = 0
    neg = False
    if n > 0 and s[0:1] == b"-":
        neg = True
        i = 1
    if i >= n:
        raise HLPanic("cannot convert string to int", line)
    v = 0
    while i < n:
        c = s[i]
        if c < 48 or c > 57:
            raise HLPanic("cannot convert string to int", line)
        v = v * 10 + (c - 48)
        if v > 2 ** 63:
            raise HLPanic("integer too large when converting string", line)
        i += 1
    if neg:
        return -v
    if v > INT64_MAX:
        raise HLPanic("integer too large when converting string", line)
    return v


# Deep-scan-25 fix (interpreter/native parity): the sign of a NaN.
# C's printf("%.6f", nan) prints "-nan" when the NaN's sign bit is set
# and "nan" otherwise, while Python's "%f" % nan ALWAYS prints "nan"
# (it ignores the sign bit). On x86-64 every freshly-created invalid
# NaN (0.0/0.0, inf-inf, sqrt(-1), fmod(x, 0), ...) has the sign bit
# SET (the hardware default QNaN is 0xFFF8000000000000), so the native
# binary printed "-nan" where the boot interpreter printed "nan".
# On aarch64 the hardware default QNaN is positive (0x7FF8000000000000),
# so the synthesised default NaN must follow the host architecture to
# stay byte-identical with the native runtime on every platform.
def _platform_default_qnan():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return float.fromhex("-nan")   # x86-64 hardware default QNaN
    return float("nan")                # aarch64/riscv64 default QNaN


_NAN_DEFAULT = _platform_default_qnan()


def fmt_float(v):
    if v != v:  # NaN — mirror C printf's sign-aware rendering
        if math.copysign(1.0, v) < 0.0:
            return b"-nan"
        return b"nan"
    return ("%.6f" % v).encode("ascii")


def to_display(b):
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b)


# Stage 9 release (v0.20.0-alpha): HalisRNG — a 64-bit LCG shared between
# the Stage-0 interpreter and the native C runtime. Same constants, same
# bit-mask → same sequence for the same seed. Critical for differential
# testing (a test using rand_seed + rand_int / rand_float must produce
# identical output in both backends; otherwise the suite would fail).
#
# Algorithm: Knuth's LCG with the glibc/MMIX Taussian-Lewis constants.
#   state = state * 6364136223846793005 + 1442695040888963407   (mod 2^64)
#   rand_int(max) = state % max   (max > 0)
#   rand_float() = (state >> 11) / 2^53   (53 bits of randomness)
# The state is masked to 64 bits with & 0xFFFFFFFFFFFFFFFF to mirror C's
# uint64_t overflow. Seed 0 is normalised to 1 because xorshift-style
# alternatives would not — but the LCG actually accepts 0 (it just stays
# at 0x...407 forever); we normalise anyway so the seed "0" does not
# produce a degenerate sequence.
class HalisRNG:
    MASK = (1 << 64) - 1
    A = 6364136223846793005
    C = 1442695040888963407

    def __init__(self):
        self.state = 1  # nonzero default; same as native runtime

    def seed(self, s):
        # HLS ints are 64-bit signed; mask to 64 bits to mirror C uint64.
        self.state = s & self.MASK
        if self.state == 0:
            self.state = 1

    def _next(self):
        self.state = (self.state * self.A + self.C) & self.MASK
        return self.state

    def randrange(self, max):
        # Caller guarantees max > 0 (the checker raises otherwise).
        return self._next() % max

    def random(self):
        # 53 bits of randomness — full precision of an IEEE double's
        # significand. Matches the native runtime's calculation.
        return (self._next() >> 11) / (1 << 53)


# Stage 21 (v0.37.0-alpha): target-feature state for has_feature() and
# the runtime CPU probe for simd_cpu_supports().
_TARGET_FEATURES = set()




__all__ = [
    "HalisRNG",
    "_NAN_DEFAULT",
    "_TARGET_FEATURES",
    "_platform_default_qnan",
    "f64_div",
    "f64_mod",
    "fmt_float",
    "i64_add",
    "i64_div",
    "i64_mod",
    "i64_mul",
    "i64_neg",
    "i64_sub",
    "parse_int",
    "to_display",
]
