#!/usr/bin/env python3
"""The package release gate — signing what the ledger can only name.

Stage 106's subject is SIGNED PACKAGES. A `publish` record names a
package and its content hash; the SBOM lists the parts; the repro
buildinfo proves the bytes come back — none of it says WHO staked
their name on the content. This module signs a release statement with
Ed25519 (the minisign discipline) and chains the signature into the
same transparency ledger every other stage appends to.

The release statement (schema hls-release-statement/v1) is canonical
JSON, content-addressed like every Stage 103–105 document:

    {
      "algorithm": "ed25519",
      "content_sha256": "...",        the digest hls-pkg publish computes
      "files": N,                     .hls files the digest walked
      "key_id": "................",   the signing key's self-derived id
      "kind": "halis-package-release",
      "lockfile_sha256": "...",       the lockfile bytes the release ships with
      "name": "...", "version": "...",
      "schema": "hls-release-statement/v1",
      "tool": "hls-sign", "tool_version": "0.125.0-alpha"
    }

No timestamp inside the signed bytes — the statement is deterministic
over an unchanged tree, the minisign trusted comment carries the wall
clock, and the ledger record carries the unix second like every other
record. One digest definition: content_sha256 is computed by the SAME
walk `hls-pkg publish` runs (sorted .hls files, rel-path \0 bytes \0),
so a signed statement and a publish record can never disagree about
what the content hash of a package is.

The gate's order is the release contract the SBOM established, with
the signature bolted in front: the manifest must exist; the audit
must pass (drift FIRST, then unauditable — fail-closed, the SBOM's
documented order); the lockfile must exist (a release ships pinned
content); the statement is built and signed; the versioned statement
and its minisign signature are written; ONE record (kind "sign") is
chained into the ledger. `verify-release` is the counterparty's side:
signature first, then the lockfile bytes, then the content digest
re-derived from the CURRENT tree — the signer signed exactly these
bytes, and this tree must still be those bytes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TOOL_DIR)

# hls-audit by path (hyphenated name) — the SBOM's loader, so the
# release signs a tree the audit itself defined and checked.
_AUDIT_PATH = os.path.join(TOOL_DIR, "hls-audit.py")
_spec = importlib.util.spec_from_file_location("hls_audit_mod_sign",
                                               _AUDIT_PATH)
hls_audit = importlib.util.module_from_spec(_spec)
sys.modules["hls_audit_mod_sign"] = hls_audit
_spec.loader.exec_module(hls_audit)

AuditError = hls_audit.AuditError

# The ledger is hls-pkg's; the sign record chains into the SAME chain
# (hpkg_parts lands on sys.path when the audit body runs).
from hpkg_log import transparency_log_append            # noqa: E402

from hlsign_parts import hlsign_format as fmt           # noqa: E402
from hlsign_parts import hlsign_ed25519 as ed           # noqa: E402

TOOL = "hls-sign"
VERSION = "0.125.0-alpha"
SCHEMA = "hls-release-statement/v1"
STATEMENT_SUFFIX = ".release.json"
SIG_SUFFIX = ".minisig"


# ---------------------------------------------------------------------------
# The one definition of a package's content digest — `hls-pkg
# publish`'s walk, kept byte-identical on purpose (the acceptance gate
# pins the two tools to the same number on the same tree).
# ---------------------------------------------------------------------------

def content_digest(pkg_dir):
    """SHA-256 over the package's .hls sources: sorted walk, rel-path
    \0 file-bytes \0 per file. Hidden dirs are skipped, exactly as the
    publish walker skips them."""
    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    sources = []
    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.endswith(".hls"):
                sources.append(os.path.join(root, fn))
    sources.sort()
    h = hashlib.sha256()
    for s in sources:
        rel = os.path.relpath(s, pkg_dir).replace(os.sep, "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        with open(s, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        h.update(b"\x00")
    return h.hexdigest(), len(sources)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The release gate.
# ---------------------------------------------------------------------------

def build_statement(pkg_dir, keyid_hex):
    """Audit the tree, require the lockfile, refuse drift, and derive
    the deterministic statement dict. The audit's order is the SBOM's:
    drift first, then unauditable — fail-closed both times."""
    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    manifest_path = os.path.join(pkg_dir, "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        raise AuditError("no hls-pkg.toml in %s — a release signs a "
                         "package" % pkg_dir)
    r = hls_audit.audit_package(pkg_dir, None)
    if r["drift"]:
        names = ", ".join(sorted({d["package"] for d in r["drift"]}))
        raise AuditError("drift between the lockfile and the tree (%s) — "
                         "re-lock; a signature over drifted content is a "
                         "lie" % names)
    if r["unauditable"]:
        names = ", ".join(sorted({u["package"] for u in r["unauditable"]}))
        raise AuditError("unauditable package(s) in the tree (%s) — fail "
                         "closed; no key signs a package nobody could "
                         "check" % names)
    if r["lockfile"] != "present":
        raise AuditError("a release ships pinned content — no lockfile "
                         "next to the manifest; run hls-pkg lock first")

    lock_path = os.path.join(pkg_dir, "hls-pkg.lock")
    content_sha, files = content_digest(pkg_dir)
    root = r["packages"][0]
    return {
        "schema": SCHEMA,
        "kind": "halis-package-release",
        "name": root["name"],
        "version": root["version"],
        "content_sha256": content_sha,
        "files": files,
        "lockfile_sha256": _sha256_file(lock_path),
        "algorithm": "ed25519",
        "key_id": keyid_hex,
        "tool": TOOL,
        "tool_version": VERSION,
    }


def statement_bytes(statement):
    """The canonical bytes that get signed — sorted keys, two-space
    indent, trailing newline; the digest the ledger records."""
    return (json.dumps(statement, indent=2, sort_keys=True) + "\n").encode(
        "utf-8")


def release(pkg_dir, secret_path, password, out_dir=None,
            trusted_comment=None):
    """Sign the release: audit → lock → statement → two signatures →
    versioned files → ONE ledger record. Returns (statement, sig_path,
    statement_path, ledger_record)."""
    seed, public, keyid, _ = fmt.read_secret(secret_path, password)
    statement = build_statement(pkg_dir, fmt.key_id_hex(public))
    payload = statement_bytes(statement)

    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    out_dir = os.path.realpath(os.path.abspath(out_dir or pkg_dir))
    if not os.path.isdir(out_dir):
        raise AuditError("output directory does not exist: %s" % out_dir)
    base = "hls-sign-%s-%s" % (statement["name"], statement["version"])
    st_path = os.path.join(out_dir, base + STATEMENT_SUFFIX)
    sig_path = os.path.join(out_dir, base + SIG_SUFFIX)

    trusted = trusted_comment or fmt.default_trusted_comment(
        base + STATEMENT_SUFFIX)
    raw_sig, global_sig = fmt.sign_file(seed, public, payload, trusted)
    with open(st_path, "wb") as f:
        f.write(payload)
    fmt.write_signature(sig_path, public, raw_sig, global_sig, trusted)

    rec = transparency_log_append({
        "kind": "sign",
        "name": statement["name"],
        "version": statement["version"],
        "content_sha256": statement["content_sha256"],
        "key_id": statement["key_id"],
        "statement_sha256": hashlib.sha256(payload).hexdigest(),
        "sig_sha256": _sha256_file(sig_path),
        "files": statement["files"],
    })
    return statement, sig_path, st_path, rec


# ---------------------------------------------------------------------------
# The counterparty's side.
# ---------------------------------------------------------------------------

def _find_release_pair(pkg_dir):
    """Exactly one statement/signature pair in the directory — two
    releases side by side are an ambiguity this tool refuses."""
    statements = sorted(fn for fn in os.listdir(pkg_dir)
                        if fn.endswith(STATEMENT_SUFFIX))
    if not statements:
        raise AuditError("no %s*%s in %s — nothing to verify"
                         % ("hls-sign-<name>-<version>", STATEMENT_SUFFIX,
                            pkg_dir))
    if len(statements) > 1:
        raise AuditError("several release statements in %s (%s) — verify "
                         "one directory per release" % (pkg_dir,
                                                        ", ".join(statements)))
    st_path = os.path.join(pkg_dir, statements[0])
    sig_path = st_path[: -len(STATEMENT_SUFFIX)] + SIG_SUFFIX
    if not os.path.isfile(sig_path):
        raise AuditError("the statement's signature is missing (%s)"
                         % os.path.basename(sig_path))
    return st_path, sig_path


def verify_release(pkg_dir, public=None, password=None, secret_path=None):
    """Verify a signed release against the CURRENT tree, in order:
    the signature over the statement bytes; the statement's shape; the
    lockfile bytes; the audit's drift check (the same fail-closed walk
    the signer ran — a lockfile the tree no longer matches is drift,
    even when its bytes are the pinned ones); the content digest
    re-derived. Any mismatch is a refusal with the broken contract
    named. Returns the statement."""
    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    st_path, sig_path = _find_release_pair(pkg_dir)

    if public is None and secret_path is not None:
        seed, public, _, _ = fmt.read_secret(secret_path, password or "")
        public = public
    if public is None:
        raise AuditError("no trust anchor — pass a public key (-p) or a "
                         "raw key (-P); a signature nobody can check "
                         "proves nothing")

    with open(st_path, "rb") as f:
        payload = f.read()
    raw_sig, global_sig, _sig_keyid, trusted = fmt.read_signature(sig_path)
    if not fmt.verify_signatures(public, payload, raw_sig, global_sig,
                                 trusted):
        raise AuditError("the release signature does not verify against "
                         "this key — the statement was tampered with or "
                         "signed by another key")

    try:
        statement = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise AuditError("the signed statement is not JSON (%s)" % ex)
    if statement.get("schema") != SCHEMA \
            or statement.get("kind") != "halis-package-release":
        raise AuditError("not a %s statement" % SCHEMA)
    if statement.get("algorithm") != "ed25519":
        raise AuditError("the statement names an unknown algorithm: %r"
                         % statement.get("algorithm"))

    lock_path = os.path.join(pkg_dir, "hls-pkg.lock")
    if not os.path.isfile(lock_path):
        raise AuditError("the lockfile is gone — the release names one")
    if _sha256_file(lock_path) != statement.get("lockfile_sha256"):
        raise AuditError("the lockfile is not the one the release names — "
                         "the pinned content changed after signing")

    # The tree must still BE the lockfile's content — the same
    # fail-closed walk the signer ran before signing anything.
    r = hls_audit.audit_package(pkg_dir, None)
    if r["drift"]:
        names = ", ".join(sorted({d["package"] for d in r["drift"]}))
        raise AuditError("drift between the lockfile and the tree (%s) — "
                         "the release's own lock no longer describes the "
                         "content" % names)

    content_sha, _files = content_digest(pkg_dir)
    if content_sha != statement.get("content_sha256"):
        raise AuditError("the tree is not what the release describes "
                         "(content digest mismatch) — drift after signing")
    if fmt.key_id_hex(public) != statement.get("key_id"):
        raise AuditError("the statement's key id does not name the "
                         "verifying key")
    return statement


__all__ = [
    "SCHEMA", "SIG_SUFFIX", "STATEMENT_SUFFIX", "TOOL",
    "VERSION", "AuditError", "build_statement", "content_digest",
    "release", "statement_bytes", "verify_release",
]
