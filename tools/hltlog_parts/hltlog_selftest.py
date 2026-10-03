#!/usr/bin/env python3
"""The hls-tlog selftest — the numbers the stage is pinned to.

Every vector here is deterministic and checked against a constant the
development run wrote down, never against the same function's own
output twice: the SHA-256 standard vectors (the empty string, "abc"),
the chain hash over a hand-built canonical record string (the
serialization order pinned, not just the hashing), the four-way gossip
classification table, the chain replay catching a tampered record, a
renumbered seq and a broken link, and the witness verdicts (rollback,
rewritten, held, byte-identical). No files, no network, no keys — the
whole test runs in memory in well under a second.
"""
from __future__ import annotations

import hashlib

from hltlog_parts.hltlog_chain import (
    AGREE, DIVERGED, FORK, LAG, build_proof, classify_ordered,
    gossip_verdict, verify_chain, verify_proof_live, verify_proof_offline,
)
from hltlog_parts.hltlog_common import GENESIS, chain_of
from hltlog_parts.hltlog_witness import check_against_log


def _record(seq, prev, kind="publish", ts=0, name="app", version="1.0.0"):
    rec = {"kind": kind, "name": name, "version": version,
           "prev_hash": prev, "seq": seq, "timestamp": ts}
    rec["chain_hash"] = chain_of(prev, rec)
    return rec


def _synthetic_log(n=3):
    recs = []
    prev = GENESIS
    for i in range(1, n + 1):
        rec = _record(i, prev, ts=1700000000 + i)
        recs.append(rec)
        prev = rec["chain_hash"]
    return recs


def selftest():
    """Run every vector; return the list of 'ok: ...' names. Raises
    AssertionError on the first broken vector — a selftest that
    silently skips is a selftest that lies."""
    names = []

    # 1. The hash itself, against the standard vectors.
    assert hashlib.sha256(b"").hexdigest() == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    names.append("SHA-256 of the empty string (FIPS vector)")
    assert hashlib.sha256(b"abc").hexdigest() == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    names.append("SHA-256 of abc (FIPS vector)")

    # 2. The chain hash over a HAND-BUILT canonical string — the
    # serialization order (sorted keys, no spaces) pinned, independent
    # of json.dumps doing it for us.
    hand = ('{"kind":"publish","name":"v","prev_hash":"'
            + GENESIS + '","seq":1,"timestamp":0}')
    want = hashlib.sha256((GENESIS + hand).encode("utf-8")).hexdigest()
    assert want == "a0e90c3bad49e0bed31d7d94eb3c148ad634d6bbe8d6c185e62dcd2a289e106d"
    rec = _record(1, GENESIS, name="v")
    del rec["version"]                       # match the hand-built body
    assert chain_of(GENESIS, rec) == want
    names.append("chain hash over the hand-built canonical record")

    # 3. The replay: a synthetic log verifies; the head is the last
    # chain hash; three tamper classes all break it.
    recs = _synthetic_log(3)
    head, errors = verify_chain(recs, "selftest")
    assert not errors and head == recs[-1]["chain_hash"]
    names.append("the replay verifies a sound log to its head")
    bad = [dict(r) for r in recs]
    bad[1] = dict(bad[1])
    bad[1]["name"] = "tampered"
    _head, errors = verify_chain(bad, "selftest")
    assert len(errors) == 1 and "chain_hash" in errors[0][1]
    names.append("a flipped record field breaks the chain")
    bad = [dict(r) for r in recs]
    bad[2] = dict(bad[2], seq=4)
    _head, errors = verify_chain(bad, "selftest")
    assert any("seq" in msg for _w, msg in errors)
    names.append("a renumbered seq is named by the replay")
    bad = [dict(r) for r in recs]
    bad[1] = dict(bad[1], prev_hash="1" * 64)
    _head, errors = verify_chain(bad, "selftest")
    assert any("prev_hash" in msg for _w, msg in errors)
    names.append("a broken link is named by the replay")

    # 4. The classification table — the whole gossip vocabulary.
    long_log = _synthetic_log(4)
    short_log = long_log[:2]
    lag_map = {r["seq"]: r["chain_hash"] for r in long_log}
    assert classify_ordered((short_log[-1]["chain_hash"], 2),
                            (long_log[-1]["chain_hash"], 4),
                            lag_map) == LAG
    same = classify_ordered((long_log[-1]["chain_hash"], 4),
                            (long_log[-1]["chain_hash"], 4), lag_map)
    assert same == AGREE
    fork_map = {1: long_log[0]["chain_hash"], 2: "b" * 64}
    assert classify_ordered((short_log[-1]["chain_hash"], 2),
                            ("c" * 64, 2), fork_map) == FORK
    renum = {1: long_log[0]["chain_hash"], 2: long_log[1]["chain_hash"],
             3: long_log[2]["chain_hash"], 4: long_log[3]["chain_hash"]}
    # The short view's head exists in the long log but at seq 3, not
    # at the length the view claims (2) — a renumbered history.
    assert classify_ordered((long_log[2]["chain_hash"], 2),
                            (long_log[3]["chain_hash"], 4),
                            renum) == FORK
    assert classify_ordered(("d" * 64, 2), (long_log[-1]["chain_hash"], 4),
                            renum) == DIVERGED
    names.append("the four-way classification table")

    # 5. The gossip reduction: consensus, lag, and the fork verdict a
    # quorum cannot outvote.
    a = {"source": "a", "length": 4, "head": long_log[-1]["chain_hash"]}
    b = {"source": "b", "length": 4, "head": long_log[-1]["chain_hash"]}
    c = {"source": "c", "length": 2, "head": short_log[-1]["chain_hash"]}
    maps = [{r["seq"]: r["chain_hash"] for r in long_log}] * 2 \
        + [lag_map]
    r = gossip_verdict([a, b, c], maps)
    assert r["verdict"] == "consensus" and r["max_lag"] == 2
    names.append("gossip reduces agreement plus lag to consensus")
    evil = {"source": "evil", "length": 4, "head": "f" * 64}
    r = gossip_verdict([a, b, evil], maps + [{1: "f" * 64}])
    assert r["verdict"] == "fork"
    names.append("one forked pair flips the whole verdict")
    r = gossip_verdict([a], maps[:1])
    assert r["verdict"] == "insufficient"
    names.append("gossip alone is not gossip (insufficient)")

    # 6. The proofs: offline coherence, and the live checks that make
    # a rewritten history refuse while a grown log still passes.
    proof = build_proof(recs, 0)
    assert not verify_proof_offline(proof)
    tam = dict(proof)
    tam["record"] = dict(proof["record"], name="evil")
    assert verify_proof_offline(tam)
    tam = dict(proof)
    tam["tail"] = [dict(t, kind="evil") for t in proof["tail"]]
    assert verify_proof_offline(tam)
    names.append("offline proof verify catches the record and the tail")
    assert not verify_proof_live(proof, recs)
    grown = _synthetic_log(5)
    assert not verify_proof_live(proof, grown)
    names.append("a proof stays valid over a grown log")
    rewritten = _synthetic_log(3)
    rewritten[0] = dict(rewritten[0], name="evil")
    rewritten[0]["chain_hash"] = chain_of(GENESIS, rewritten[0])
    assert verify_proof_live(proof, rewritten)
    names.append("a rewritten history kills the proof")

    # 7. The witness verdicts — the rollback line gossip cannot draw.
    view_w = {"length": 3, "head": recs[-1]["chain_hash"],
              "log_sha256": "a" * 64}
    w = dict(make_witness_dict(view_w))
    r = check_against_log(w, recs, "a" * 64)
    assert r["verdict"] == "byte-identical"
    r = check_against_log(w, recs[:2])
    assert r["verdict"] == "rollback"
    r = check_against_log(w, recs + [_record(4, recs[-1]["chain_hash"])])
    assert r["verdict"] == "held"
    short2 = _synthetic_log(2)
    forked = short2 + [_record(3, short2[-1]["chain_hash"], name="evil")]
    assert forked[2]["chain_hash"] != recs[2]["chain_hash"]
    r = check_against_log(w, forked)
    assert r["verdict"] == "rewritten"
    names.append("the witness verdicts: rollback, rewritten, held, "
                 "byte-identical")

    return names


def make_witness_dict(view):
    """The witness fields check_against_log reads — no key material,
    no signature (that layer is fmt's, tested by Stage 106's gate and
    the acceptance gate here)."""
    return {
        "schema": "hls-tlog-witness/v1",
        "length": view["length"],
        "head": view["head"],
        "log_sha256": view["log_sha256"],
        "kinds": {},
        "algorithm": "ed25519",
        "key_id": "0" * 16,
        "tool": "hls-tlog",
        "tool_version": "0.126.0-alpha",
    }


__all__ = ["selftest"]
