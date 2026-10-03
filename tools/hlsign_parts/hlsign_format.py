#!/usr/bin/env python3
"""Key and signature file formats — the minisign contract, honestly.

PUBLIC KEY FILE (minisign-compatible, byte for byte):

    untrusted comment: minisign public key <KEYID> <comment>
    <base64 of: "Ed" (2) || key id (8) || public key (32)>

Any tool that reads minisign public keys reads this file. The key id
is not random: it is the first 8 bytes of SHA-256 over the public
key — self-deriving, so a ledger record or a release statement can
NAME the signing key without shipping the key file, and two files
claiming the same key id cannot disagree about the key.

SIGNATURE FILE (minisign-compatible, byte for byte):

    untrusted comment: signature from hls-sign key <KEYID>
    <base64 of: "Ed" (2) || signature over the payload (64)>
    trusted comment: <one line, signed>
    <base64 of: "Ed" (2) || signature over (raw sig || trusted)>

Two signatures, like minisign: the first over the payload, the second
over the payload signature CONCATENATED WITH the trusted comment — so
rewriting the human-readable comment after the fact breaks the file.
The trusted comment carries the timestamp and the file name (minisign's
own convention); it is signed, but it is not trusted-by-default — the
"untrusted"/"trusted" split is minisign's, and the payload is the only
thing the verifier's verdict is about.

SECRET KEY FILE (the honest divergence):

    untrusted comment: hls-sign secret key <KEYID> <comment>
    <base64 of the encrypted box>

minisign encrypts its secret key with libsodium (scrypt +
XSalsa20-Poly1305); the toolchain has no libsodium, so the box here is
the Stage 106 construction, documented in full:

    "hs" (2)                    format marker — Halis secret key, v1
    "PD" (2)                    KDF id: PBKDF2-HMAC-SHA256
    iterations (4, big-endian)  the KDF cost
    salt (16)                   the KDF salt
    nonce (12)                  the AEAD nonce
    box = ChaCha20-Poly1305(key = KDF(passphrase, salt, iterations),
                            nonce, aad = b"hls-sign-key", plaintext)

    plaintext (82 bytes) = "Ed" (2) || key id (8) || seed (32)
                           || public key (32) || checksum (8)

    checksum = SHA-256(seed || public key)[:8]

The AEAD already refuses a wrong passphrase; the checksum catches a
corrupted box before any key material is derived, and the re-derived
public key must equal the stored one before the key is used. The
passphrase may be empty (minisign allows it too); the KDF still runs.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import time

from hlsign_parts import hlsign_aead as aead
from hlsign_parts import hlsign_ed25519 as ed

SIG_ALGO = b"Ed"
SECRET_MARKER = b"hs"
KDF_ID = b"PD"
KDF_AAD = b"hls-sign-key"
DEFAULT_ITERATIONS = 200000
DEFAULT_PUB_NAME = "hls-sign.pub"
DEFAULT_SECRET_NAME = "hls-sign.key"
PUB_COMMENT = "minisign public key"


class SignError(Exception):
    """A file, key or signature the format layer refuses."""


# ---------------------------------------------------------------------------
# Encoding helpers.
# ---------------------------------------------------------------------------

def b64(data):
    return base64.b64encode(data).decode("ascii")


def unb64(text, what):
    try:
        return base64.b64decode(text.strip(), validate=True)
    except Exception as ex:
        raise SignError("%s is not valid base64 (%s)" % (what, ex))


def key_id(public):
    """The self-deriving key id: SHA-256(public)[:8], hex-encoded for
    display. Two bytes of key file never decide trust — the signature
    itself does — the id only NAMES the key consistently everywhere."""
    return hashlib.sha256(public).digest()[:8]


def key_id_hex(public):
    return key_id(public).hex()


# ---------------------------------------------------------------------------
# Public key file.
# ---------------------------------------------------------------------------

def public_blob(public):
    return SIG_ALGO + key_id(public) + public


def write_public(path, public, comment=""):
    header = "untrusted comment: %s %s" % (PUB_COMMENT, key_id_hex(public))
    if comment:
        header += " " + comment.replace("\n", " ")
    with open(path, "w") as f:
        f.write(header + "\n" + b64(public_blob(public)) + "\n")
    return path


def read_public(path):
    """Parse a public key file; return (public_bytes, keyid_bytes,
    comment). The blob's key id must agree with the key it names."""
    try:
        with open(path, "r") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError as ex:
        raise SignError("cannot read the public key %s (%s)" % (path, ex))
    if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
        raise SignError("%s is not a public key file" % path)
    blob = unb64(lines[1], "the public key blob")
    if len(blob) != 42 or blob[:2] != SIG_ALGO:
        raise SignError("the public key blob is not Ed(2) + id(8) + pk(32)")
    public = blob[10:]
    if blob[2:10] != key_id(public):
        raise SignError("the public key file's id does not name its key")
    return public, blob[2:10], lines[0][len("untrusted comment:"):].strip()


# ---------------------------------------------------------------------------
# Secret key file — always encrypted, the passphrase through the KDF.
# ---------------------------------------------------------------------------

def _secret_plaintext(public, seed):
    return (SIG_ALGO + key_id(public) + seed + public
            + hashlib.sha256(seed + public).digest()[:8])


def write_secret(path, seed, public, password, iterations=DEFAULT_ITERATIONS,
                 comment=""):
    plaintext = _secret_plaintext(public, seed)
    salt = os.urandom(16)
    nonce = aead.random_nonce()
    box_key = aead.kdf_key(password, salt, iterations)
    ct, tag = aead.aead_encrypt(box_key, nonce, KDF_AAD, plaintext)
    blob = (SECRET_MARKER + KDF_ID + iterations.to_bytes(4, "big")
            + salt + nonce + ct + tag)
    header = "untrusted comment: hls-sign secret key %s" % key_id_hex(public)
    if comment:
        header += " " + comment.replace("\n", " ")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(header + "\n" + b64(blob) + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, always
    except OSError:
        pass
    return path


def read_secret(path, password):
    """Open the box, check the checksum and the key id, re-derive the
    public key, and return (seed, public, keyid, comment). A wrong
    passphrase or a corrupted file is SignError — never a bad key."""
    try:
        with open(path, "r") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError as ex:
        raise SignError("cannot read the secret key %s (%s)" % (path, ex))
    if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
        raise SignError("%s is not a secret key file" % path)
    blob = unb64(lines[1], "the secret key box")
    marker, kdf_id = blob[:2], blob[2:4]
    if marker != SECRET_MARKER:
        raise SignError("the secret key box is not a Halis key (marker %r)"
                        % marker)
    if kdf_id != KDF_ID:
        raise SignError("the secret key's KDF (%r) is not supported" % kdf_id)
    iterations = int.from_bytes(blob[4:8], "big")
    salt, nonce = blob[8:24], blob[24:36]
    body, tag = blob[36:-16], blob[-16:]
    try:
        box_key = aead.kdf_key(password, salt, iterations)
        plaintext = aead.aead_decrypt(box_key, nonce, KDF_AAD, body, tag)
    except aead.AeadError as ex:
        raise SignError("the secret key cannot be opened (%s)" % ex)
    if len(plaintext) != 82 or plaintext[:2] != SIG_ALGO:
        raise SignError("the decrypted secret key is not the expected box")
    keyid = plaintext[2:10]
    seed, public = plaintext[10:42], plaintext[42:74]
    checksum = plaintext[74:82]
    if hashlib.sha256(seed + public).digest()[:8] != checksum:
        raise SignError("the secret key's checksum does not match")
    if ed.public_from_seed(seed) != public:
        raise SignError("the secret key's seed does not derive its public "
                        "key")
    if keyid != key_id(public):
        raise SignError("the secret key's id does not name its key")
    return seed, public, keyid, lines[0][len("untrusted comment:"):].strip()


# ---------------------------------------------------------------------------
# Minisign signature files.
# ---------------------------------------------------------------------------

def default_trusted_comment(path):
    return "timestamp:%d\tfile:%s" % (
        int(time.time()),
        os.path.basename(path).replace("\n", " "))


def write_signature(path, public, raw_sig, global_sig, trusted_comment):
    lines = [
        "untrusted comment: signature from hls-sign key %s"
        % key_id_hex(public),
        b64(SIG_ALGO + raw_sig),
        "trusted comment: %s" % trusted_comment.replace("\n", " "),
        b64(SIG_ALGO + global_sig),
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def read_signature(path):
    """Parse the four-line minisign contract; return (raw_sig,
    global_sig, keyid_hex, trusted_comment). Structure failures are
    SignErrors naming the line that broke it."""
    try:
        with open(path, "r") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError as ex:
        raise SignError("cannot read the signature %s (%s)" % (path, ex))
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:"):
        raise SignError("%s is not a signature file" % path)
    raw_blob = unb64(lines[1], "the signature line")
    if len(raw_blob) != 66 or raw_blob[:2] != SIG_ALGO:
        raise SignError("the signature line is not Ed(2) + sig(64)")
    if not lines[2].startswith("trusted comment: "):
        raise SignError("the third line of a signature file is the trusted "
                        "comment")
    trusted = lines[2][len("trusted comment: "):]
    global_blob = unb64(lines[3], "the trusted-comment signature line")
    if len(global_blob) != 66 or global_blob[:2] != SIG_ALGO:
        raise SignError("the trusted-comment signature is not Ed(2) + sig(64)")
    untrusted = lines[0][len("untrusted comment:"):]
    keyid = ""
    if "key" in untrusted:
        keyid = untrusted.rsplit("key", 1)[1].strip().split(" ")[0]
    return raw_blob[2:], global_blob[2:], keyid, trusted


def sign_file(seed, public, payload, trusted_comment):
    """The two signatures: one over the payload, one over
    (payload signature || trusted comment) — minisign's order."""
    raw_sig = ed.sign(seed, payload)
    global_sig = ed.sign(seed, raw_sig + trusted_comment.encode("utf-8"))
    return raw_sig, global_sig


def verify_signatures(public, payload, raw_sig, global_sig, trusted_comment):
    """Both signatures must hold: the payload's AND the comment's."""
    return (ed.verify(public, payload, raw_sig)
            and ed.verify(public, raw_sig + trusted_comment.encode("utf-8"),
                          global_sig))


__all__ = [
    "DEFAULT_ITERATIONS", "DEFAULT_PUB_NAME", "DEFAULT_SECRET_NAME",
    "KDF_AAD", "SignError", "SIG_ALGO", "b64", "default_trusted_comment",
    "key_id", "key_id_hex", "read_public", "read_secret", "read_signature",
    "sign_file", "unb64", "verify_signatures", "write_public",
    "write_secret", "write_signature",
]
