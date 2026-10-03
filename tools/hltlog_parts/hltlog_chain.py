#!/usr/bin/env python3
"""The chain, the gossip classification and the inclusion proofs.

Three pieces of Stage 107's contract live here:

VERIFY (one log). Replay from genesis: record 1 must chain from
"0"*64, every record must chain from the one before it (the recomputed
hash must equal the stored chain_hash), and the seqs must count
1..N with no gaps and no repeats. The replay returns the head — the
chain_hash the last record carries — and every break with its line.
A linear chain has no Merkle shortcut: verification IS the replay,
and the replay is cheap (a few KB per record, thousands of records in
the practical limit).

GOSSIP (many sources). Each source serves a log; each log yields a
view (length + head). Two views compare in exactly one way — the
classification every pair runs through:

  same length, same head                -> AGREE
  same length, different head           -> FORK
  shorter's head == longer's chain_hash
    at the shorter's length             -> LAG (the mirror is behind)
  shorter's head found in the longer
    at any OTHER seq                    -> FORK (a renumbered history;
                                           unreachable for SOUND logs
                                           — the chain hash binds the
                                           seq — kept as defense for
                                           callers that pass unverified
                                           maps; pinned by the
                                           selftest's unit vectors)
  shorter's head nowhere in the longer  -> DIVERGED

LAG is the healthy answer for mirrors: byte the lag is, the shorter's
whole history is still inside the longer's. A source is never asked to
trust another source — the classification only compares hashes the
chain itself pinned, and any single FORK or DIVERGED pair flips the
gossip verdict (a quorum cannot outvote evidence).

PROOF (one record). A linear chain makes inclusion a replay too, so a
proof carries the record, every record after it (the tail), and the
head they chain to. Offline verification recomputes the record's own
chain hash and replays the tail to the head. Live verification
(--log) does the real thing: replay the log from genesis and require
the record byte-equal at its seq and the head still a prefix of the
live log's — a proof stays valid over a GROWN log and dies on a
rewritten one.
"""
from __future__ import annotations

from hltlog_parts.hltlog_common import (
    GENESIS, PROOF_SCHEMA, TlogError, chain_of, canon_bytes,
)

# The relationship between two views of one log — the whole gossip
# vocabulary, kept as strings so reports and --json agree.
AGREE = "agree"
LAG = "lag"
FORK = "fork"
DIVERGED = "diverged"


# ---------------------------------------------------------------------------
# Verify: the replay.
# ---------------------------------------------------------------------------

def verify_chain(records: list, origin: str = "<log>"):
    """Replay the chain. Returns (head, errors): head is the last
    record's chain_hash (GENESIS for an empty log); errors is a list
    of (line-ish name, message) — empty when the log is sound. The
    replay continues after a break so one report names every break in
    the file, not just the first."""
    errors = []
    prev = GENESIS
    expect_seq = 1
    head = GENESIS
    for i, rec in enumerate(records):
        where = "%s: record %d" % (origin, i + 1)
        if rec.get("seq") != expect_seq:
            errors.append((where, "seq %r out of order (want %d)"
                           % (rec.get("seq"), expect_seq)))
            # Re-sync the expected numbering to what the file claims so
            # the rest of the report stays meaningful.
            expect_seq = rec["seq"] if isinstance(rec.get("seq"), int) \
                else expect_seq
        if rec.get("prev_hash") != prev:
            errors.append((where, "prev_hash does not chain to the "
                           "record before it"))
        want = chain_of(rec.get("prev_hash", GENESIS), rec)
        if rec.get("chain_hash") != want:
            errors.append((where, "chain_hash does not match the "
                           "record's content (recomputed %s...)"
                           % want[:12]))
        prev = rec.get("chain_hash", prev)
        head = rec.get("chain_hash", head)
        expect_seq += 1
    return head, errors


# ---------------------------------------------------------------------------
# Gossip: the pairwise classification and the verdict.
# ---------------------------------------------------------------------------

def classify_ordered(shorter, longer, longer_heads_by_seq) -> str:
    """The classification, in the only direction it means anything:
    is the SHORTER view's history contained in the LONGER's?"""
    s_head, s_len = shorter
    l_head, l_len = longer
    if s_len == l_len:
        return AGREE if s_head == l_head else FORK
    at = longer_heads_by_seq.get(s_len)
    if at == s_head:
        return LAG
    if s_head in set(longer_heads_by_seq.values()):
        return FORK           # the head exists, renumbered
    return DIVERGED


def gossip_verdict(views: list, heads_by_seq_per_view: list) -> dict:
    """Compare every pair of ok views and reduce to one verdict.

    `views` is a list of dicts with `source`, `length`, `head`;
    `heads_by_seq_per_view` the matching seq->hash maps. Deterministic:
    sources keep their given order, pairs run (i, j) with i < j, and
    the verdict reduces over ALPHABETICALLY sorted class names so the
    report never depends on dict ordering.

    Returns {verdict, head, max_lag, pairs} where `pairs` lists every
    conflicting pair (and, when nothing conflicts, the lagging ones
    worth reporting). The verdict is one of: insufficient (fewer than
    two ok sources), consensus (every pair agrees or lags), fork (any
    FORK/DIVERGED pair exists).
    """
    pairs = []
    worst = AGREE
    max_lag = 0
    for i in range(len(views)):
        for j in range(i + 1, len(views)):
            a, b = views[i], views[j]
            if a["length"] <= b["length"]:
                cls = classify_ordered((a["head"], a["length"]),
                                       (b["head"], b["length"]),
                                       heads_by_seq_per_view[j])
                lag = b["length"] - a["length"] if cls == LAG else 0
            else:
                cls = classify_ordered((b["head"], b["length"]),
                                       (a["head"], a["length"]),
                                       heads_by_seq_per_view[i])
                lag = a["length"] - b["length"] if cls == LAG else 0
            if cls in (FORK, DIVERGED):
                lag = 0
            max_lag = max(max_lag, lag)
            entry = {"a": a["source"], "b": b["source"], "class": cls}
            if cls == LAG:
                entry["lag"] = lag
            pairs.append(entry)
            # The reduction: evidence outranks health. DIVERGED is a
            # fork with the shared history missing, not a milder word.
            if cls in (FORK, DIVERGED):
                worst = FORK
            elif cls == LAG and worst == AGREE:
                worst = LAG
    verdict = {
        "insufficient": "insufficient",
        AGREE: "consensus",
        LAG: "consensus",
        FORK: "fork",
    }[worst if len(views) >= 2 else "insufficient"]
    longest = max(views, key=lambda v: (v["length"], v["head"])) \
        if views else None
    return {
        "verdict": verdict,
        "head": longest["head"] if longest else GENESIS,
        "max_lag": max_lag,
        "pairs": pairs,
    }


# ---------------------------------------------------------------------------
# Inclusion proofs.
# ---------------------------------------------------------------------------

def build_proof(records: list, index: int) -> dict:
    """The proof for records[index] (0-based): the record, the tail
    after it, the view it chains to. The caller has already verified
    the chain — a proof is only ever built from a sound log."""
    rec = records[index]
    tail = records[index + 1:]
    return {
        "schema": PROOF_SCHEMA,
        "kind": "halis-tlog-inclusion",
        "seq": rec["seq"],
        "record": rec,
        "tail": tail,
        "length": len(records),
        "head": records[-1]["chain_hash"] if records else GENESIS,
        "tool": "hls-tlog",
    }


def verify_proof_offline(proof: dict) -> list:
    """The proof's internal contract, no log needed: the record's
    chain_hash recomputes from its own fields; the tail chains from the
    record to the head. Returns the list of breaks (empty = coherent)."""
    errors = []
    rec = proof.get("record")
    if not isinstance(rec, dict):
        return [("proof", "no record in the proof")]
    want = chain_of(rec.get("prev_hash", GENESIS), rec)
    if rec.get("chain_hash") != want:
        errors.append(("proof record", "chain_hash does not match the "
                       "record's content"))
    prev = rec.get("chain_hash")
    for k, t in enumerate(proof.get("tail") or []):
        want_t = chain_of(prev, t) if prev else "?"
        if prev and t.get("chain_hash") != want_t:
            errors.append(("tail %d" % (k + 1), "chain_hash does not "
                           "chain from the record before it"))
        prev = t.get("chain_hash", prev)
    head = prev if prev else GENESIS
    if proof.get("head") != head:
        errors.append(("proof head", "the tail does not end at the "
                       "head the proof names"))
    # The log-length claim: the record sits at its seq, everything
    # after it is the tail — so the length must be exactly seq + tail.
    if proof.get("seq") is not None \
            and proof.get("length") != proof["seq"] + len(proof.get("tail")
                                                          or []):
        errors.append(("proof length", "the length does not cover the "
                       "record's position plus its tail"))
    return errors


def verify_proof_live(proof: dict, records: list) -> list:
    """The real inclusion check against a live log: the proof's record
    sits byte-equal (canonical JSON) at its seq in the log, and the
    proof's head is still a prefix of the log — valid over a GROWN
    log, dead on a rewritten one. Returns the breaks."""
    errors = verify_proof_offline(proof)
    if errors:
        return errors
    seq = proof["seq"]
    live = None
    for rec in records:
        if rec.get("seq") == seq:
            live = rec
            break
    if live is None:
        errors.append(("live log", "seq %d is not in the log — the "
                       "record is gone" % seq))
        return errors
    if canon_bytes(live) != canon_bytes(proof["record"]):
        errors.append(("live log", "seq %d is a different record — "
                       "the history was rewritten" % seq))
        return errors
    live_heads = {rec["seq"]: rec["chain_hash"] for rec in records}
    if live_heads.get(proof["length"]) != proof["head"]:
        errors.append(("live log", "the proof's head is not the log's "
                       "record %d anymore — the history was rewritten"
                       % proof["length"]))
    return errors


__all__ = [
    "AGREE", "DIVERGED", "FORK", "LAG",
    "build_proof", "classify_ordered", "gossip_verdict",
    "verify_chain", "verify_proof_live", "verify_proof_offline",
    "canon_bytes",
]
