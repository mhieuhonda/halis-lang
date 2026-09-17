"""hlserve_tls — Stage 75 self-signed cert generation.

When the user passes ``--https`` (or sets ``https = true`` in the
config file), the dev server generates an ephemeral self-signed
certificate in-memory and serves HTTPS. The cert is valid for
``localhost`` and ``127.0.0.1`` only (a dev server should not be
reachable from the internet).

Implementation strategy:

* If the optional ``cryptography`` library is available, generate a
  proper RSA-2048 self-signed X.509 cert and write it (plus its key)
  to a temp file pair. ``ssl.SSLContext.load_cert_chain`` loads them.
* If ``cryptography`` is NOT installed, fall back to plain HTTP and
  warn. (Forcing a dependency on ``cryptography`` would break the
  Stage 75 "no new requirements" contract.)

The cert is regenerated on each server start (no on-disk cache) so a
different dev session has a different keypair — good hygiene for a dev
server, even if the cert's not actually trusted by the browser.
"""
from __future__ import annotations

import os
import ssl
import sys
import tempfile
import time
from typing import Optional, Tuple

from hlserve_common import warn, info


def _have_cryptography() -> bool:
    try:
        import cryptography  # type: ignore[import-not-found]
        return True
    except ImportError:
        return False


def generate_self_signed_cert(common_name: str = "localhost"
                              ) -> Optional[Tuple[str, str]]:
    """Generate a self-signed cert + key. Returns ``(cert_path,
    key_path)`` if successful, None if ``cryptography`` is not
    installed.

    The cert is written to a temp file pair (so ``ssl.SSLContext`` can
    load them by path). The files are marked ``chmod 600``.
    """
    if not _have_cryptography():
        warn("--https requested but the 'cryptography' package is not "
             "installed; falling back to plain HTTP. "
             "(pip install cryptography)")
        return None
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime as _dt

    info("generating self-signed cert (this is normal on first --https "
         "run)…")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "ZZ"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "hls-serve dev"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_dt.datetime.utcnow())
            .not_valid_after(_dt.datetime.utcnow()
                              + _dt.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.DNSName(common_name),
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256()))
    # Write to temp files.
    cert_fd, cert_path = tempfile.mkstemp(prefix="hlserve-cert-",
                                          suffix=".pem")
    key_fd, key_path = tempfile.mkstemp(prefix="hlserve-key-",
                                        suffix=".pem")
    with os.fdopen(cert_fd, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with os.fdopen(key_fd, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    os.chmod(cert_path, 0o600)
    os.chmod(key_path, 0o600)
    return (cert_path, key_path)


def make_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Build an SSLContext suitable for ``http.server.HTTPServer``.

    The context uses TLSv1.2+ (the minimum that supports modern cipher
    suites). Client cert verification is OFF (we're a server, not a
    client-cert gateway).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def cleanup_cert_pair(pair: Optional[Tuple[str, str]]) -> None:
    """Delete the temp cert files (called on shutdown)."""
    if pair is None:
        return
    for p in pair:
        try:
            os.unlink(p)
        except OSError:
            pass
