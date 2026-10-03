#!/usr/bin/env python3
"""Ed25519 — pure-Python implementation of RFC 8032 (Stage 106).

The whole toolchain is stdlib-only, so the signing stage carries its
own Ed25519: no libsodium, no cryptography wheel. The implementation
is the RFC's own arithmetic — the Edwards25519 curve in extended
homogeneous coordinates, SHA-512 for the hashing, the standard clamped
scalar — small enough to audit in one sitting and pinned to the RFC's
test vectors by `selftest()` (the gate runs it; so can you:
`python3 tools/hls-sign.py selftest`).

Determinism is the property the stage leans on: RFC 8032 signing
derives the nonce r from the MESSAGE and the seed prefix, never from a
random generator, so signing the same bytes with the same key twice
produces the same signature — a signature file can be re-emitted and
compared, and the acceptance gate pins the RFC's exact signature bytes.

Honest limits, stated where they belong: pure Python is NOT
constant-time (the interpreter's branches and allocations leak timing),
so this module is for signing and verifying on a developer's machine
and in CI — the same trust boundary the rest of the Python toolchain
already assumes — not for a network service holding keys against a
timing adversary.
"""
from __future__ import annotations

import hashlib
import os

# ---------------------------------------------------------------------------
# Curve constants (RFC 8032, section 5.1).
# ---------------------------------------------------------------------------

P = 2 ** 255 - 19                      # the field prime
L = 2 ** 252 + 27742317777372353535851937790883648493   # the group order
D = (-121665 * pow(121666, P - 2, P)) % P               # the curve constant
_SQRT_M1 = pow(2, (P - 1) // 4, P)     # sqrt(-1) in the field


def _x_of(y):
    """The x coordinate recovery: x^2 = (y^2 - 1) / (d*y^2 + 1). None
    when the point is not on the curve."""
    num = (y * y - 1) % P
    den = (D * y * y + 1) % P
    x2 = num * pow(den, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    return x


# The base point B (the RFC's decimal coordinates, kept verbatim so the
# constant is checkable against the RFC text). The module's own sanity:
# BY is on the curve and BX is one of its two roots.
BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_x = _x_of(BY)
assert _x is not None and _x * _x % P == (BX * BX) % P
BASE = (BX, BY, 1, BX * BY % P)
IDENTITY = (0, 1, 1, 0)


class Ed25519Error(Exception):
    """A key, signature or encoding the module refuses."""


# ---------------------------------------------------------------------------
# Point arithmetic in extended homogeneous coordinates (X, Y, Z, T),
# the formulas verbatim from RFC 8032 section 5.1.4.
# ---------------------------------------------------------------------------

def _pt_add(p1, p2):
    X1, Y1, Z1, T1 = p1
    X2, Y2, Z2, T2 = p2
    A = (Y1 - X1) * (Y2 - X2) % P
    B = (Y1 + X1) * (Y2 + X2) % P
    C = T1 * 2 * D * T2 % P
    Dd = Z1 * 2 * Z2 % P
    E = B - A
    F = Dd - C
    G = Dd + C
    H = B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _pt_double(p):
    X1, Y1, Z1, _ = p
    A = X1 * X1 % P
    B = Y1 * Y1 % P
    C = 2 * Z1 * Z1 % P
    H = (A + B) % P
    E = (H - (X1 + Y1) * (X1 + Y1)) % P
    G = (A - B) % P
    F = (C + G) % P
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _scalarmult(p, e):
    """[e]p by double-and-add from the top bit. Plain Python is not
    constant-time; see the module docstring for where that is honest."""
    if e < 0:
        raise Ed25519Error("negative scalar")
    q = IDENTITY
    for i in range(255, -1, -1):
        q = _pt_double(q)
        if (e >> i) & 1:
            q = _pt_add(q, p)
    return q


def _compress(p):
    X, Y, Z, _ = p
    zinv = pow(Z, P - 2, P)
    x = X * zinv % P
    y = Y * zinv % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(b32):
    if len(b32) != 32:
        return None
    val = int.from_bytes(b32, "little")
    sign = (val >> 255) & 1
    y = val & ((1 << 255) - 1)
    if y >= P:
        return None
    x = _x_of(y)
    if x is None:
        return None
    if x == 0 and sign == 1:
        return None
    if (x & 1) != sign:
        x = P - x
    return (x, y, 1, x * y % P)


# ---------------------------------------------------------------------------
# Keys, signing, verifying (RFC 8032 section 5.1.5 / 5.1.6 / 5.1.7).
# ---------------------------------------------------------------------------

def _sha512(data):
    return hashlib.sha512(data).digest()


def _clamp(prefix32):
    a = bytearray(prefix32)
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(bytes(a), "little")


def secret_expand(seed):
    """The seed's clamped scalar and the prefix the nonce derivation
    reads — the ONLY place a seed becomes a signing scalar."""
    if len(seed) != 32:
        raise Ed25519Error("the seed is 32 bytes")
    h = _sha512(seed)
    return _clamp(h[:32]), h[32:]


def public_from_seed(seed):
    a, _ = secret_expand(seed)
    return _compress(_scalarmult(BASE, a))


def keypair():
    """A fresh (seed, pk) pair — os.urandom is the only randomness in
    the module, and it is used exactly here."""
    seed = os.urandom(32)
    return seed, public_from_seed(seed)


def sign(seed, msg):
    """RFC 8032 5.1.6 — deterministic: r comes from SHA-512(prefix ||
    msg), never from an RNG. Same seed, same bytes, same signature."""
    a, prefix = secret_expand(seed)
    pk = _compress(_scalarmult(BASE, a))
    r = int.from_bytes(_sha512(prefix + msg), "little") % L
    r_enc = _compress(_scalarmult(BASE, r))
    k = int.from_bytes(_sha512(r_enc + pk + msg), "little") % L
    s = (r + k * a) % L
    return r_enc + s.to_bytes(32, "little")


def verify(pk, msg, sig):
    """RFC 8032 5.1.7 — the cofactored equation [8][S]B == [8]R +
    [8][k]A, computed over compressed bytes. Returns True/False; a
    malformed anything is False, never an exception."""
    if len(sig) != 64:
        return False
    a_pt = _decompress(pk)
    r_pt = _decompress(sig[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= L:
        return False
    k = int.from_bytes(_sha512(sig[:32] + pk + msg), "little") % L
    left = _scalarmult(_scalarmult(BASE, s), 8)
    right = _scalarmult(_pt_add(r_pt, _scalarmult(a_pt, k)), 8)
    return _compress(left) == _compress(right)


# ---------------------------------------------------------------------------
# The RFC 8032 section 7.1 test vectors (keys, messages, signatures in
# hex). selftest() runs sign AND verify against every one of them and
# refuses the module if a single byte disagrees.
# ---------------------------------------------------------------------------

_VECTORS = [
    # TEST 1 — the empty message.
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    # TEST 2 — one byte.
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    # TEST 3 — two bytes.
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    # TEST SHA(abc) — the message is sha512(b"abc") (64 bytes), both
    # halves of the module (sign determinism, verify acceptance) pinned
    # by one vector.
    ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
     "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
     "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


def selftest():
    """Run the RFC 8032 vectors; return the names passed. Raises
    AssertionError with the failing vector's name on any mismatch."""
    passed = []
    for i, (seed_hex, pk_hex, msg_hex, sig_hex) in enumerate(_VECTORS, 1):
        name = "RFC 8032 vector %d" % i
        seed = bytes.fromhex(seed_hex)
        pk = bytes.fromhex(pk_hex)
        msg = bytes.fromhex(msg_hex)
        sig = bytes.fromhex(sig_hex)
        assert public_from_seed(seed) == pk, "%s: public key mismatch" % name
        produced = sign(seed, msg)
        assert produced == sig, "%s: signature mismatch (determinism)" % name
        assert verify(pk, msg, sig), "%s: verify refused a good signature" % name
        # One flipped signature byte must refuse.
        bad = bytearray(sig)
        bad[10] ^= 0x40
        assert not verify(pk, msg, bytes(bad)), \
            "%s: verify accepted a flipped byte" % name
        # One flipped message byte must refuse.
        if msg:
            bad_msg = bytearray(msg)
            bad_msg[0] ^= 0x01
            assert not verify(pk, bytes(bad_msg), sig), \
                "%s: verify accepted a flipped message byte" % name
        passed.append(name)
    # A fresh pair round-trips; a foreign key refuses.
    seed, pk = keypair()
    msg = os.urandom(37)
    sig = sign(seed, msg)
    assert verify(pk, msg, sig)
    _other, other_pk = keypair()
    assert not verify(other_pk, msg, sig), "a foreign key must refuse"
    passed.append("fresh keypair round-trip")
    passed.append("foreign key refusal")
    return passed


__all__ = [
    "BASE", "Ed25519Error", "L", "P", "IDENTITY",
    "keypair", "public_from_seed", "secret_expand", "selftest",
    "sign", "verify",
]
