#!/usr/bin/env python3
"""Witnesses — a view with a name behind it, the Stage 106 keys.

A view is a claim about a log, and Stage 106 taught the toolchain what
a claim without a name is worth. A WITNESS is a view signed with the
same Ed25519 keypair `hls-sign` generated — the minisign file
discipline, the same two-signature contract, no new crypto. The wall
clock rides the SIGNED trusted comment (`timestamp:<unix>`), never the
signed bytes: like every Stage 103–106 document, the witness is
deterministic over an unchanged log, so two witnesses over the same
log differ only when their signers wanted them to.

The witness is what makes ROLLBACK detectable — the one attack plain
gossip cannot see. A mirror missing the newest records is a lagging
mirror (healthy); a mirror missing records SOMEONE SAW AND SIGNED is
a rolled-back log (evidence). `check_against_log` draws exactly that
line:

  current length >= witness length, head matches at that seq   -> held
  current length == witness length AND the bytes are identical  -> byte-identical
  current length >= witness length, head does NOT match        -> rewritten
  current length <  witness length                             -> rollback
"""
from __future__ import annotations

import json
import os

from hltlog_parts.hltlog_common import (
    TOOL, VERSION, WITNESS_SCHEMA, TlogError, canon_bytes,
)

from hlsign_parts import hlsign_format as fmt          # noqa: E402


def make_witness(view: dict, public: bytes) -> dict:
    """The witness document over a view: the view's fields plus the
    signer's algorithm and key id. No timestamp inside the signed
    bytes; no source path either — a witness is about the log, not
    about where the log was found."""
    if view.get("schema") != "hls-tlog-view/v1":
        raise TlogError("a witness signs a hls-tlog-view/v1 view, not "
                        "%r" % view.get("schema"))
    return {
        "schema": WITNESS_SCHEMA,
        "kind": "halis-tlog-witness",
        "length": view["length"],
        "head": view["head"],
        "log_sha256": view["log_sha256"],
        "kinds": view["kinds"],
        "algorithm": "ed25519",
        "key_id": fmt.key_id_hex(public),
        "tool": TOOL,
        "tool_version": VERSION,
    }


def witness_base(view: dict) -> str:
    """The content-named file stem: hls-tlog-witness-<length>-<head16>.
    Deterministic, no clock in the name — two witnesses over the same
    log collide on purpose (same bytes), and --force decides."""
    return "hls-tlog-witness-%d-%s" % (view["length"], view["head"][:16])


def write_witness(witness: dict, seed: bytes, public: bytes,
                  out_dir: str, trusted_comment: str, force: bool = False):
    """Sign the canonical witness bytes and write the pair: the JSON
    document plus its detached minisign signature. Returns the paths."""
    payload = canon_bytes(witness)
    base = witness_base({
        "length": witness["length"], "head": witness["head"],
    })
    doc_path = os.path.join(out_dir, base + ".witness.json")
    sig_path = doc_path + ".minisig"
    for p in (doc_path, sig_path):
        if os.path.exists(p) and not force:
            raise TlogError("%s already exists — a witness over the "
                            "same view is the same bytes (--force to "
                            "re-write)" % p)
    raw_sig, global_sig = fmt.sign_file(seed, public, payload,
                                        trusted_comment)
    os.makedirs(out_dir, exist_ok=True)
    with open(doc_path, "wb") as f:
        f.write(payload)
    fmt.write_signature(sig_path, public, raw_sig, global_sig,
                        trusted_comment)
    return doc_path, sig_path


def read_witness(path: str):
    """Read the witness document + its signature. Returns
    (witness_dict, trusted_comment)."""
    if not os.path.isfile(path):
        raise TlogError("no witness at %s" % path)
    sig_path = path + ".minisig"
    if not os.path.isfile(sig_path):
        raise TlogError("no signature at %s — an unsigned witness is a "
                        "rumor with a file name" % sig_path)
    try:
        doc = json.load(open(path, encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise TlogError("%s is not JSON (%s)" % (path, ex))
    if not isinstance(doc, dict) or doc.get("schema") != WITNESS_SCHEMA:
        raise TlogError("%s is not a %s document" % (path, WITNESS_SCHEMA))
    for field in ("length", "head", "log_sha256", "kinds", "algorithm",
                  "key_id"):
        if field not in doc:
            raise TlogError("%s has no %s — not a witness this tool "
                            "wrote" % (path, field))
    _raw, _glob, _kid, trusted = fmt.read_signature(sig_path)
    return doc, trusted


def verify_witness_signature(doc_path: str, public: bytes) -> bool:
    """The minisign contract over the witness bytes, Stage 106's own
    verifier — payload signature AND trusted-comment signature."""
    with open(doc_path, "rb") as f:
        payload = f.read()
    sig_path = doc_path + ".minisig"
    raw_sig, global_sig, _kid, trusted = fmt.read_signature(sig_path)
    return fmt.verify_signatures(public, payload, raw_sig, global_sig,
                                 trusted)


def trusted_timestamp(trusted: str):
    """The signed trusted comment carries `timestamp:<unix>` — parse it
    out for the report. None when the signer did not stamp it."""
    for part in (trusted or "").split():
        if part.startswith("timestamp:"):
            raw = part.split(":", 1)[1]
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def check_against_log(witness: dict, records: list,
                      log_sha256: str = None) -> dict:
    """The witness against the CURRENT log. Returns
    {verdict, detail}: held / byte-identical / rewritten / rollback.
    The verdict only ever comes from hashes the chain pinned — a log
    the caller has not verified is the caller's bug, and the CLI
    verifies before it calls. `log_sha256` (the current log bytes'
    digest) sharpens the held verdict: the same length AND the same
    bytes is the byte-identical answer."""
    w_len = witness["length"]
    w_head = witness["head"]
    cur_len = len(records)
    if cur_len < w_len:
        detail = ("the log holds %d record(s), the witness signed %d — "
                  "record(s) %d..%d are gone under a signed promise"
                  % (cur_len, w_len, cur_len + 1, w_len))
        return {"verdict": "rollback", "detail": detail}
    at = None
    for rec in records:
        if rec.get("seq") == w_len:
            at = rec
            break
    if at is None:
        return {"verdict": "rewritten",
                "detail": "seq %d is not in the log — the history was "
                          "renumbered under the witness" % w_len}
    if at.get("chain_hash") != w_head:
        return {"verdict": "rewritten",
                "detail": "seq %d carries a different chain_hash than "
                          "the witness signed — the history was "
                          "rewritten" % w_len}
    if cur_len == w_len:
        if log_sha256 is not None and log_sha256 == witness["log_sha256"]:
            return {"verdict": "byte-identical",
                    "detail": "the log IS the witnessed bytes"}
        return {"verdict": "held", "detail": "the log is exactly the "
                "witnessed length with the witnessed head"}
    return {"verdict": "held", "detail": "the witnessed history is a "
            "prefix of the log (%d of %d record(s))"
            % (w_len, cur_len)}


__all__ = [
    "check_against_log", "make_witness", "read_witness",
    "trusted_timestamp", "verify_witness_signature", "witness_base",
    "write_witness",
]
