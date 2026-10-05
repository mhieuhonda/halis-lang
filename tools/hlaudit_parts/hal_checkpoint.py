"""Signed checkpoints — a name staked on the audit log's head.

An audit log no one has signed is a log whose operator can rewrite
at leisure: the chain detects mutation of any PAST record, but only
a signature pins the fact that the past reached exactly (length,
head) — the property that turns truncation into evidence. A
checkpoint is that signature: an Ed25519 document (the Stage 106
keys, the minisign two-signature file discipline, no new crypto)
over the canonical bytes of

    {
      "schema": "hls-audit-checkpoint/v1",
      "kind": "halis-audit-checkpoint",
      "length": 12,               the entries the signer saw
      "head": "<chain_hash>",     the chain hash of entry 12
      "log_sha256": "<digest>",   the raw log bytes at signing time
      "ops": {...},               the per-op inventory the log held
      "outcomes": {"ok": 10, "refused": 2},
      "actors": {...},
      "algorithm": "ed25519",
      "key_id": "................",
      "tool": "hls-auditlog", "tool_version": "0.131.0-alpha"
    }

No timestamp inside the signed bytes — deterministic over an
unchanged log, like every Stage 103–107 document; the minisign
trusted comment carries the wall clock, and the log itself carries
the unix second (the checkpoint op's own entry lands right after the
document it names — the log describes its own checkpointing, and
verify-sign reads the verdict off hashes, never off the self-report).

The verdict vocabulary is the witness's (Stage 107), because the
question is the same question:

  log shorter than the checkpoint                     -> rollback
  head missing / different at the checkpoint length   -> rewritten
  same length, same bytes                             -> byte-identical
  the witnessed history a prefix of a grown log       -> held
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_TOOLS_DIR,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hlaudit_parts.hal_common import (                # noqa: E402
    CHECKPOINT_KIND, CHECKPOINT_SCHEMA, CHECKPOINT_SUFFIX, SIG_SUFFIX,
    AuditlogError, TOOL, VERSION, canon_bytes,
)
from hlaudit_parts.hal_log import summarize           # noqa: E402

from hlsign_parts import hlsign_format as fmt         # noqa: E402


def build_checkpoint(records: list, log_sha256: str, keyid_hex: str) -> dict:
    """The checkpoint document over a VERIFIED log (the caller has
    replayed the chain first — this tool never signs a log it has
    not checked). The inventory comes from the same `summarize` the
    reports run, so a checkpoint and a verify report cannot
    disagree about what the log held."""
    inv = summarize(records)
    head = records[-1]["chain_hash"] if records else "0" * 64
    return {
        "schema": CHECKPOINT_SCHEMA,
        "kind": CHECKPOINT_KIND,
        "length": len(records),
        "head": head,
        "log_sha256": log_sha256,
        "ops": inv["ops"],
        "outcomes": inv["outcomes"],
        "actors": inv["actors"],
        "algorithm": "ed25519",
        "key_id": keyid_hex,
        "tool": TOOL,
        "tool_version": VERSION,
    }


def checkpoint_base(ckpt: dict) -> str:
    """The content-named file stem: hls-audit-ckpt-<length>-<head16>.
    Deterministic, no clock in the name — two checkpoints over the
    same head collide on purpose (same bytes), and --force decides."""
    return "hls-audit-ckpt-%d-%s" % (ckpt["length"], ckpt["head"][:16])


def write_checkpoint(ckpt: dict, seed: bytes, public: bytes,
                     out_dir: str, trusted_comment: str,
                     force: bool = False):
    """Sign the canonical checkpoint bytes and write the pair: the
    JSON document plus its detached minisign signature. Returns the
    paths."""
    payload = canon_bytes(ckpt)
    base = checkpoint_base(ckpt)
    doc_path = os.path.join(out_dir, base + CHECKPOINT_SUFFIX)
    sig_path = doc_path + SIG_SUFFIX
    for p in (doc_path, sig_path):
        if os.path.exists(p) and not force:
            raise AuditlogError("%s already exists — a checkpoint over "
                                "the same head is the same bytes "
                                "(--force to re-write)" % p)
    raw_sig, global_sig = fmt.sign_file(seed, public, payload,
                                        trusted_comment)
    os.makedirs(out_dir, exist_ok=True)
    with open(doc_path, "wb") as f:
        f.write(payload)
    fmt.write_signature(sig_path, public, raw_sig, global_sig,
                        trusted_comment)
    return doc_path, sig_path


def read_checkpoint(path: str):
    """Read the checkpoint document + its signature. Returns
    (checkpoint_dict, trusted_comment)."""
    if not os.path.isfile(path):
        raise AuditlogError("no checkpoint at %s" % path)
    sig_path = path + SIG_SUFFIX
    if not os.path.isfile(sig_path):
        raise AuditlogError("no signature at %s — an unsigned "
                            "checkpoint is a rumor with a file name"
                            % sig_path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (UnicodeDecodeError, ValueError) as ex:
        raise AuditlogError("%s is not JSON (%s)" % (path, ex))
    if not isinstance(doc, dict) or doc.get("schema") != CHECKPOINT_SCHEMA:
        raise AuditlogError("%s is not a %s document"
                            % (path, CHECKPOINT_SCHEMA))
    for field in ("length", "head", "log_sha256", "ops", "outcomes",
                  "algorithm", "key_id"):
        if field not in doc:
            raise AuditlogError("%s has no %s — not a checkpoint this "
                                "tool wrote" % (path, field))
    _raw, _glob, _kid, trusted = fmt.read_signature(sig_path)
    return doc, trusted


def verify_checkpoint_signature(doc_path: str, public: bytes) -> bool:
    """The minisign contract over the checkpoint bytes, Stage 106's
    own verifier — payload signature AND trusted-comment signature."""
    with open(doc_path, "rb") as f:
        payload = f.read()
    raw_sig, global_sig, _kid, trusted = fmt.read_signature(
        doc_path + SIG_SUFFIX)
    return fmt.verify_signatures(public, payload, raw_sig, global_sig,
                                 trusted)


def check_against_log(ckpt: dict, records: list,
                      log_sha256: str = None) -> dict:
    """The checkpoint against the CURRENT log — the verdict only ever
    comes from hashes the chain pinned, so the caller has verified
    the log before asking. Returns {verdict, detail}: the witness
    vocabulary, drawn for the audit log."""
    c_len = ckpt["length"]
    c_head = ckpt["head"]
    cur_len = len(records)
    if cur_len < c_len:
        detail = ("the log holds %d entry(ies), the checkpoint signed "
                  "%d — entry(ies) %d..%d are gone under a signed "
                  "promise" % (cur_len, c_len, cur_len + 1, c_len))
        return {"verdict": "rollback", "detail": detail}
    at = None
    for rec in records:
        if rec.get("seq") == c_len:
            at = rec
            break
    if at is None:
        return {"verdict": "rewritten",
                "detail": "seq %d is not in the log — the history was "
                          "renumbered under the checkpoint" % c_len}
    if at.get("chain_hash") != c_head:
        return {"verdict": "rewritten",
                "detail": "seq %d carries a different chain_hash than "
                          "the checkpoint signed — the history was "
                          "rewritten" % c_len}
    if cur_len == c_len:
        if log_sha256 is not None and log_sha256 == ckpt["log_sha256"]:
            return {"verdict": "byte-identical",
                    "detail": "the log IS the checkpointed bytes"}
        return {"verdict": "held",
                "detail": "the log is exactly the checkpointed length "
                          "with the checkpointed head"}
    return {"verdict": "held",
            "detail": "the checkpointed history is a prefix of the log "
                      "(%d of %d entry(ies))" % (c_len, cur_len)}


def log_sha256_of(path: str) -> str:
    """The raw log bytes' digest — what the checkpoint pins so two
    logs at the same head can still be told byte-identical apart."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "build_checkpoint", "check_against_log", "checkpoint_base",
    "log_sha256_of", "read_checkpoint", "verify_checkpoint_signature",
    "write_checkpoint",
]
