#!/usr/bin/env python3
"""ChaCha20-Poly1305 + PBKDF2 — the AEAD box the secret key rests in.

Stage 106 needs the minisign PROPERTY — a secret key file that is
useless without the passphrase — and the toolchain is stdlib-only, so
the box is built from the two primitives the RFC 8439 construction
defines: the ChaCha20 stream cipher and the Poly1305 authenticator,
composed exactly as the RFC's AEAD composes them (one-time key from
block 0, encryption from block 1 up, MAC over AAD || ct || lengths).
The passphrase stretches through PBKDF2-HMAC-SHA256 (hashlib's C
implementation — the one place this module leans on the stdlib's
optimized code).

`selftest()` pins the module to the RFC's own numbers: the block
function (2.3.2), the Poly1305 example (2.5.2), the one-time key
generation (2.6.2) and the full AEAD round trip (2.8.2) — the exact
sunscreen ciphertext, byte for byte. Tamper refusal is part of the
contract: a flipped ciphertext byte or a flipped AAD byte must fail
the MAC, never silently decrypt.
"""
from __future__ import annotations

import hashlib
import hmac
import os


class AeadError(Exception):
    """A decryption the MAC refused."""


# ---------------------------------------------------------------------------
# ChaCha20 (RFC 8439 section 2.3).
# ---------------------------------------------------------------------------

_MASK32 = 0xFFFFFFFF
_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


def _rotl32(v, n):
    return ((v << n) & _MASK32) | (v >> (32 - n))


def _quarter_round(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _MASK32
    s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _MASK32
    s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _MASK32
    s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _MASK32
    s[b] = _rotl32(s[b] ^ s[c], 7)


def _keystream_block(key32, counter, nonce12):
    """One 64-byte keystream block — the RFC's state, 20 rounds, plus
    the original state, serialized little-endian."""
    if len(key32) != 32:
        raise AeadError("the ChaCha20 key is 32 bytes")
    if len(nonce12) != 12:
        raise AeadError("the ChaCha20 nonce is 12 bytes")
    s = list(_CONSTANTS) + [
        int.from_bytes(key32[i:i + 4], "little") for i in range(0, 32, 4)
    ] + [
        counter & _MASK32,
        int.from_bytes(nonce12[0:4], "little"),
        int.from_bytes(nonce12[4:8], "little"),
        int.from_bytes(nonce12[8:12], "little"),
    ]
    work = list(s)
    for _ in range(10):
        _quarter_round(work, 0, 4, 8, 12)
        _quarter_round(work, 1, 5, 9, 13)
        _quarter_round(work, 2, 6, 10, 14)
        _quarter_round(work, 3, 7, 11, 15)
        _quarter_round(work, 0, 5, 10, 15)
        _quarter_round(work, 1, 6, 11, 12)
        _quarter_round(work, 2, 7, 8, 13)
        _quarter_round(work, 3, 4, 9, 14)
    return b"".join(((work[i] + s[i]) & _MASK32).to_bytes(4, "little")
                    for i in range(16))


def chacha20_xor(key32, counter, nonce12, data):
    """The stream cipher: keystream blocks from counter upward, XORed
    over the data. Encryption and decryption are the same function."""
    out = bytearray()
    block_index = 0
    while block_index * 64 < len(data):
        ks = _keystream_block(key32, counter + block_index, nonce12)
        chunk = data[block_index * 64:(block_index + 1) * 64]
        out.extend(b ^ k for b, k in zip(chunk, ks))
        block_index += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# Poly1305 (RFC 8439 section 2.5).
# ---------------------------------------------------------------------------

_POLY_P = (1 << 130) - 5
_POLY_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def poly1305(key32, msg):
    """The one-time authenticator: 16-byte little-endian blocks, each
    with the 0x01 high byte, folded into the clamped-r accumulator."""
    if len(key32) != 32:
        raise AeadError("the Poly1305 key is 32 bytes")
    r = int.from_bytes(key32[:16], "little") & _POLY_CLAMP
    s = int.from_bytes(key32[16:32], "little")
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i:i + 16] + b"\x01"
        acc = ((acc + int.from_bytes(block, "little")) * r) % _POLY_P
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def poly1305_key_gen(key32, nonce12):
    """The one-time MAC key: block 0 of the keystream (RFC 2.6.1)."""
    return _keystream_block(key32, 0, nonce12)[:32]


# ---------------------------------------------------------------------------
# The AEAD composition (RFC 8439 section 2.8).
# ---------------------------------------------------------------------------

def _pad16(data):
    rem = len(data) % 16
    return b"" if rem == 0 else b"\x00" * (16 - rem)


def aead_encrypt(key32, nonce12, aad, plaintext):
    """Encrypt: (ciphertext, 16-byte tag). The MAC covers the AAD, the
    ciphertext and BOTH lengths, padded as the RFC's construction says."""
    otk = poly1305_key_gen(key32, nonce12)
    ct = chacha20_xor(key32, 1, nonce12, plaintext)
    mac_data = (aad + _pad16(aad) + ct + _pad16(ct)
                + len(aad).to_bytes(8, "little")
                + len(ct).to_bytes(8, "little"))
    return ct, poly1305(otk, mac_data)


def aead_decrypt(key32, nonce12, aad, ct, tag):
    """Decrypt with the MAC checked FIRST (compare_digest — the tag is
    compared in constant time where Python allows it); any tamper with
    the ciphertext or the AAD raises AeadError before a byte returns."""
    otk = poly1305_key_gen(key32, nonce12)
    mac_data = (aad + _pad16(aad) + ct + _pad16(ct)
                + len(aad).to_bytes(8, "little")
                + len(ct).to_bytes(8, "little"))
    if not hmac.compare_digest(poly1305(otk, mac_data), tag):
        raise AeadError("the AEAD tag does not verify — the box was "
                        "tampered with or the passphrase is wrong")
    return chacha20_xor(key32, 1, nonce12, ct)


def random_nonce():
    """A fresh 12-byte nonce — os.urandom, used once per encryption."""
    return os.urandom(12)


# ---------------------------------------------------------------------------
# The passphrase KDF.
# ---------------------------------------------------------------------------

def kdf_key(password, salt16, iterations):
    """PBKDF2-HMAC-SHA256 to 32 bytes — the box key."""
    if iterations < 1:
        raise AeadError("the KDF needs at least one iteration")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt16, iterations, dklen=32)


# ---------------------------------------------------------------------------
# The RFC 8439 test vectors.
# ---------------------------------------------------------------------------

_VECTORS = {
    # Section 2.3.2 — the block function, key 00..1f, counter 1,
    # nonce 000000090000004a00000000, the whole 64-byte block pinned.
    "block":
        ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
         "000000090000004a00000000", 1,
         "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
         "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e"),
    # Section 2.5.2 — the Poly1305 example over
    # "Cryptographic Forum Research Group".
    "poly1305":
        ("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b",
         b"Cryptographic Forum Research Group",
         "a8061dc1305136c6c22b8baf0c0127a9"),
    # Section 2.6.2 — the one-time key generation.
    "otk":
        ("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f",
         "000000000001020304050607",
         "8ad5a08b905f81cc815040274ab29471a833b637e3fd0da508dbb8e2fdd1a646"),
    # Section 2.8.2 — the full AEAD: the sunscreen sentence, the exact
    # ciphertext and the exact tag.
    "aead_key":
        "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f",
    "aead_nonce": "070000004041424344454647",
    "aead_aad": "50515253c0c1c2c3c4c5c6c7",
    "aead_pt":
        b"Ladies and Gentlemen of the class of '99: If I could offer you "
        b"only one tip for the future, sunscreen would be it.",
    "aead_ct":
        "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
        "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
        "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
        "3ff4def08e4b7a9de576d26586cec64b6116",
    "aead_tag": "1ae10b594f09e26a7e902ecbd0600691",
}


def selftest():
    """Run the RFC 8439 vectors + the tamper refusals; return the names
    passed. AssertionError names the vector on any mismatch."""
    k, n, c, want = _VECTORS["block"]
    got = _keystream_block(bytes.fromhex(k), c, bytes.fromhex(n))
    assert got == bytes.fromhex(want), "block: keystream mismatch"

    k, msg, want = _VECTORS["poly1305"]
    got = poly1305(bytes.fromhex(k), msg)
    assert got == bytes.fromhex(want), "poly1305: tag mismatch"

    k, n, want = _VECTORS["otk"]
    got = poly1305_key_gen(bytes.fromhex(k), bytes.fromhex(n))
    assert got == bytes.fromhex(want), "otk: one-time key mismatch"

    key = bytes.fromhex(_VECTORS["aead_key"])
    nonce = bytes.fromhex(_VECTORS["aead_nonce"])
    aad = bytes.fromhex(_VECTORS["aead_aad"])
    pt = _VECTORS["aead_pt"]
    ct, tag = aead_encrypt(key, nonce, aad, pt)
    assert ct == bytes.fromhex(_VECTORS["aead_ct"]), "aead: ciphertext mismatch"
    assert tag == bytes.fromhex(_VECTORS["aead_tag"]), "aead: tag mismatch"
    assert aead_decrypt(key, nonce, aad, ct, tag) == pt, "aead: round trip"
    # Tamper with the ciphertext: the MAC must refuse.
    bad = bytearray(ct)
    bad[0] ^= 0x01
    try:
        aead_decrypt(key, nonce, aad, bytes(bad), tag)
        raise AssertionError("aead: a flipped ciphertext byte decrypted")
    except AeadError:
        pass
    # Tamper with the AAD: the MAC must refuse.
    bad_aad = bytearray(aad)
    bad_aad[0] ^= 0x01
    try:
        aead_decrypt(key, nonce, bytes(bad_aad), ct, tag)
        raise AssertionError("aead: a flipped AAD byte decrypted")
    except AeadError:
        pass
    return ["RFC 8439 block", "RFC 8439 poly1305", "RFC 8439 otk",
            "RFC 8439 AEAD + tamper refusals"]


__all__ = [
    "AeadError", "aead_decrypt", "aead_encrypt", "chacha20_xor",
    "kdf_key", "poly1305", "poly1305_key_gen", "random_nonce", "selftest",
]
