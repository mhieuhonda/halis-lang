#!/usr/bin/env python3
"""Stage 107 acceptance gate — hls-tlog, the transparency-log gossip
protocol (multi-source verify).

Run with `make tlog-acceptance` (or
`python3 tests/tlog_acceptance.py`).

Nine sections:

  1. the primitives — the SHA-256 standard vectors, the chain hash
                      over a hand-built canonical record, the ONE
                      DEFINITION against hpkg_log's own writer, the
                      CLI selftest
  2. the views       — the view's fields, determinism over unchanged
                      bytes, the empty log, verify/summary exits and
                      the refusal wording
  3. the sources     — file, directory (the ledger inside), a local
                      HTTP URL, and the refusals (missing file, a
                      directory without a ledger, non-JSON, a broken
                      shape, the size cap, a 404)
  4. the gossip      — consensus, lag, fork, diverged, renumbered,
                      the missing source, --require, --max-lag, the
                      json verdict, the directory source in the mix
  5. the witnesses   — the versioned pair, determinism, verify
                      against the log (byte-identical, grown, the
                      rollback, the well-formed fork), tampered
                      witness, wrong key, missing signature,
                      sign-before-verify on a broken log
  6. the proofs      — prove by name, by name+version, by seq, the
                      missing record, prove-verify offline / live /
                      grown / rewritten at the record, the tampered
                      record and tail, the garbage shape
  7. the ledger      — a real package: hls-pkg lock + publish; the
                      repo log grows; hls-tlog verify agrees with
                      hls-pkg log --verify; the summary head IS the
                      last chain hash; gossip over the ledger and a
                      copy; prove the publish record live
  8. the demos       — the demo and the ok-test run; interpreter and
                      native agree byte for byte (native half only
                      when bin/hlc exists — the gate stays hermetic)
  9. the tools       — hlfmt stable, hllint clean on the new entry
                      sources
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

TLOG = [sys.executable, os.path.join(ROOT, "tools", "hls-tlog.py")]
SIGN = [sys.executable, os.path.join(ROOT, "tools", "hls-sign.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
DEMO = "examples/tlog_demo.hls"
OK_TEST = "tests/ok/feat_stage107_tlog.hls"
PW_VAR = "HLS_SIGN_PASSWORD"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s107_", dir=os.path.join(ROOT, "tests"))
LOG_LOCK = os.path.join(ROOT, ".hls-pkg-transparency.log.lock")
LOG_MAIN = os.path.join(ROOT, ".hls-pkg-transparency.log")
DEP_DIR = os.path.join(ROOT, "tests", "_gate_s107_dep")


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


def expect(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def tlog(*extra, env=None):
    return run(TLOG + list(extra), env=env or os.environ)


def tlog_json(*extra, env=None):
    p = tlog(*(list(extra) + ["--json"]), env=env)
    rep = None
    if p.stdout:
        try:
            rep = json.loads(p.stdout)
        except ValueError:
            rep = None
    return p, rep


def log_lines():
    if not os.path.exists(LOG_MAIN):
        return 0
    with open(LOG_MAIN, "rb") as f:
        return len(f.readlines())


# The machinery, imported directly — the gate tests the modules and
# the CLI over them, so a CLI bug cannot hide behind a module pass.
from hltlog_parts import hltlog_chain as ch                # noqa: E402
from hltlog_parts import hltlog_common as com              # noqa: E402
from hltlog_parts import hltlog_selftest as st             # noqa: E402
from hltlog_parts import hltlog_witness as wit             # noqa: E402
from hpkg_log import _build_chained_record                 # noqa: E402

PW_ENV = {PW_VAR: "gate-passphrase-107"}


def make_log(path, n=4, name="app", start=1, prev=com.GENESIS,
             ts0=1700000000, append=False):
    """A well-formed synthetic ledger fragment; returns (records,
    prev) so callers can chain more onto it. append=True continues an
    existing file (the grown-log fixtures)."""
    recs = []
    for i in range(start, start + n):
        rec = {"kind": "publish" if i % 2 else "sign", "name": name,
               "version": "1.%d.0" % i, "prev_hash": prev, "seq": i,
               "timestamp": ts0 + i}
        rec["chain_hash"] = com.chain_of(prev, rec)
        recs.append(rec)
        prev = rec["chain_hash"]
    with open(path, "a" if append else "w") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return recs, prev


# ---------------------------------------------------------------------------
print("=== 1. the primitives (the chain arithmetic, one definition) ===")
# ---------------------------------------------------------------------------

names = st.selftest()
expect(len(names) == 15, "the selftest runs all fifteen vectors in-memory")

import hashlib
expect(hashlib.sha256(b"abc").hexdigest() ==
       "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
       "the SHA-256 the chain hash leans on is the standard one")

# ONE DEFINITION: any record through hpkg_log's own writer must
# re-verify under hls-tlog's arithmetic, byte for byte.
probe = {"kind": "publish", "name": "probe", "version": "9.9.9"}
built = _build_chained_record(dict(probe), com.GENESIS)
mine = com.chain_of(built["prev_hash"],
                    {k: v for k, v in built.items() if k != "chain_hash"})
expect(built["chain_hash"] == mine,
       "hls-tlog's chain hash IS hpkg_log's writer (one definition)")
expect(built["chain_hash"] == com.chain_of(
    built["prev_hash"], {k: v for k, v in built.items()
                         if k != "chain_hash"}),
       "the built record re-verifies under the shared arithmetic")

p = tlog("selftest")
expect(p.returncode == 0 and p.stdout.count("  ok: ") == 15
       and "hand-built canonical record" in p.stdout,
       "the CLI selftest reports all fifteen vectors (exit 0)")

# ---------------------------------------------------------------------------
print("=== 2. the views (the summary every witness signs) ===")
# ---------------------------------------------------------------------------

views_dir = os.path.join(TMP, "views")
os.makedirs(views_dir)
log_a = os.path.join(views_dir, "a.json")
recs_a, head_a = make_log(log_a, 4)

view = com.view_for_path(log_a)
expect(view["schema"] == com.VIEW_SCHEMA and view["length"] == 4
       and view["head"] == head_a and view["first_seq"] == 1
       and view["last_seq"] == 4 and view["kinds"] == {"publish": 2,
                                                       "sign": 2},
       "the view's fields: length, head, kinds, seq range")
expect(view["log_sha256"] == sha256_of(log_a),
       "the view pins the log's raw bytes")
view2 = com.view_for_path(log_a)
expect(view == view2, "the view is deterministic over unchanged bytes")

empty = os.path.join(views_dir, "empty.json")
write(empty, "")
ev = com.view_for_path(empty)
expect(ev["length"] == 0 and ev["head"] == com.GENESIS
       and ev["kinds"] == {},
       "an empty log views as length 0 with the genesis head")

p, rep = tlog_json("summary", log_a)
expect(p.returncode == 0 and rep["schema"] == com.VIEW_SCHEMA
       and rep["view"]["length"] == 4 and rep["sound"] is True,
       "summary --json reports the verified view")

p = tlog("verify", log_a)
expect(p.returncode == 0 and "chain is sound" in p.stdout,
       "verify: a sound log exits 0 and says so")

tam = os.path.join(views_dir, "tam.json")
recs_t, _ = make_log(tam, 3)
with open(tam, "a") as f:
    evil = dict(recs_t[-1])
    evil["name"] = "evil"
    evil["chain_hash"] = com.chain_of(evil["prev_hash"], evil)
    f.write(json.dumps(evil, sort_keys=True) + "\n")
p = tlog("verify", tam)
expect(p.returncode == 1 and "prev_hash does not chain" in p.stdout,
       "verify: a grafted record is named by the broken link")

bad_json = os.path.join(views_dir, "bad.json")
write(bad_json, "{not json\n")
p = tlog("verify", bad_json)
expect(p.returncode == 1 and "line 1 is not JSON" in p.stderr,
       "verify: a non-JSON line refuses with the line number")

bad_shape = os.path.join(views_dir, "shape.json")
write(bad_shape, json.dumps({"kind": "publish"}) + "\n")
p = tlog("verify", bad_shape)
expect(p.returncode == 1 and "bad seq" in p.stderr,
       "verify: a record without the mandatory shape refuses")

# ---------------------------------------------------------------------------
print("=== 3. the sources (file, directory, URL) ===")
# ---------------------------------------------------------------------------

from hltlog_parts import hltlog_sources as srcs             # noqa: E402

label, data, reason = srcs.fetch(log_a)
expect(reason is None and data == open(log_a, "rb").read(),
       "the file source serves the bytes")

pkg_dir_source = os.path.join(TMP, "pkgdir")
os.makedirs(pkg_dir_source)
shutil.copyfile(log_a, os.path.join(pkg_dir_source, srcs.LEDGER_NAME))
p, rep = tlog_json("gossip", pkg_dir_source, pkg_dir_source)
expect(p.returncode == 0 and rep["verdict"] == "consensus"
       and rep["ok_sources"] == 2,
       "a directory source resolves to the ledger inside it")

label, _data, reason = srcs.fetch(os.path.join(TMP, "nope.json"))
expect(reason is not None and "no such file" in reason,
       "a missing file source is refused with the reason")

dirless = os.path.join(TMP, "dirless")
os.makedirs(dirless)
label, _data, reason = srcs.fetch(dirless)
expect(reason is not None and "no .hls-pkg-transparency.log" in reason,
       "a directory without a ledger is refused")

# A local HTTP source — hermetic: an ephemeral port on 127.0.0.1,
# serving the fixture bytes, shut down before the section ends.
http_logs = {"good": open(log_a, "rb").read(), "bad": b"{oops\n"}
served_404 = {"path": None}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = http_logs.get(self.path.strip("/"))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):      # keep the gate output clean
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    url = "http://127.0.0.1:%d/good" % port
    p, rep = tlog_json("gossip", log_a, url)
    expect(p.returncode == 0 and rep["verdict"] == "consensus"
           and rep["ok_sources"] == 2,
           "an HTTP URL is a first-class gossip source")
    bad_url = "http://127.0.0.1:%d/bad" % port
    p, rep = tlog_json("gossip", log_a, bad_url)
    expect(p.returncode == 1 and rep["ok_sources"] == 1
           and any("not JSON" in (s.get("error") or "")
                   for s in rep["sources"]),
           "a URL serving garbage is a reported refusal, not a crash")
    nf_url = "http://127.0.0.1:%d/absent" % port
    p, rep = tlog_json("gossip", log_a, nf_url)
    expect(p.returncode == 1 and rep["ok_sources"] == 1
           and any("HTTP Error 404" in (s.get("error") or "")
                   for s in rep["sources"]),
           "a 404 URL is a reported refusal")
    p, rep = tlog_json("gossip", log_a, url, "--max-bytes", "10")
    expect(p.returncode == 1
           and any("byte cap" in (s.get("error") or "")
                   for s in rep["sources"]),
           "the size cap refuses an oversized source")
finally:
    server.shutdown()
    server.server_close()

# ---------------------------------------------------------------------------
print("=== 4. the gossip (the multi-source verdict) ===")
# ---------------------------------------------------------------------------

g_dir = os.path.join(TMP, "gossip")
os.makedirs(g_dir)
base = os.path.join(g_dir, "base.json")
recs_g, head_g = make_log(base, 4, name="gossip")
copy1 = os.path.join(g_dir, "copy1.json")
shutil.copyfile(base, copy1)
copy2 = os.path.join(g_dir, "copy2.json")
shutil.copyfile(base, copy2)

p, rep = tlog_json("gossip", base, copy1, copy2)
expect(p.returncode == 0 and rep["verdict"] == "consensus"
       and rep["max_lag"] == 0 and len(rep["pairs"]) == 3,
       "three identical sources: consensus, three agree pairs")

lag = os.path.join(g_dir, "lag.json")
make_log(lag, 2, name="gossip", ts0=1700000000)
p, rep = tlog_json("gossip", base, lag)
expect(p.returncode == 0 and rep["verdict"] == "consensus"
       and rep["max_lag"] == 2
       and any(pr["class"] == "lag" and pr.get("lag") == 2
               for pr in rep["pairs"]),
       "a lagging mirror is consensus with the lag named")

fork = os.path.join(g_dir, "fork.json")
recs_f, _ = make_log(fork, 4, name="gossip")
with open(fork, "w") as f:
    for r in recs_f[:3]:
        f.write(json.dumps(r, sort_keys=True) + "\n")
    evil = dict(recs_f[3])
    evil["name"] = "evil"
    evil["chain_hash"] = com.chain_of(evil["prev_hash"], evil)
    f.write(json.dumps(evil, sort_keys=True) + "\n")
p = tlog("gossip", base, fork)
expect(p.returncode == 1 and "CONFLICT" in p.stdout and "fork" in p.stdout,
       "same length, different history: the pair is named as a fork")
p, rep = tlog_json("gossip", base, fork, copy1)
expect(rep["verdict"] == "fork",
       "the verdict stays fork with a quorum on one side")

div = os.path.join(g_dir, "div.json")
make_log(div, 3, name="other", ts0=1600000000)
p, rep = tlog_json("gossip", base, div)
expect(p.returncode == 1 and rep["verdict"] == "fork"
       and any(pr["class"] == "diverged" for pr in rep["pairs"]),
       "an unrelated history is diverged (fork evidence)")

p = tlog("gossip", base, os.path.join(g_dir, "absent.json"))
expect(p.returncode == 1 and "insufficient" in p.stdout
       and "1 of 2" in p.stdout,
       "a missing source is reported; the verdict is insufficient")

p, rep = tlog_json("gossip", base, "--require", "1")
expect(p.returncode == 0 and rep["verdict"] == "isolated",
       "--require 1 names the isolation instead of failing it")

p, rep = tlog_json("gossip", base, lag, "--max-lag", "1")
expect(p.returncode == 1 and rep["verdict"] == "stale",
       "--max-lag turns excessive lag into a refusal")

# A fork must not be downgraded by a generous --max-lag: evidence
# outranks health, always.
p, rep = tlog_json("gossip", base, fork, "--max-lag", "1000")
expect(p.returncode == 1 and rep["verdict"] == "fork",
       "--max-lag never downgrades a fork")

# A refused source never kills the conversation: two good sources
# still reach consensus with the refusal reported.
garbage_log = os.path.join(g_dir, "garbage.json")
write(garbage_log, "not json at all\n")
p, rep = tlog_json("gossip", base, copy1, garbage_log)
expect(p.returncode == 0 and rep["verdict"] == "consensus"
       and rep["ok_sources"] == 2
       and any(not s["ok"] for s in rep["sources"]),
       "a refused source is reported; the healthy ones still agree")

p = tlog("gossip")
expect(p.returncode == 2, "gossip with no sources is a usage error")

# ---------------------------------------------------------------------------
print("=== 5. the witnesses (the signed view) ===")
# ---------------------------------------------------------------------------

w_dir = os.path.join(TMP, "witness")
os.makedirs(w_dir)
w_log = os.path.join(w_dir, "w.json")
recs_w, head_w = make_log(w_log, 4, name="wit")
key = os.path.join(w_dir, "hls-sign.key")
pub = os.path.join(w_dir, "hls-sign.pub")
run(SIGN + ["keygen", "-f", key, "-p", pub, "-c", "gate fixture",
            "--password-env", PW_VAR, "--iterations", "1000"],
    env=dict(os.environ, **PW_ENV))

p = tlog("witness", w_log, "--sign", key, "--out", w_dir,
         "--trusted-comment", "timestamp:0", env=dict(os.environ, **PW_ENV))
expect(p.returncode == 0, "witness signs the verified view (exit 0)")
wfile = os.path.join(
    w_dir, "hls-tlog-witness-4-%s.witness.json" % head_w[:16])
wsig = wfile + ".minisig"
expect(os.path.isfile(wfile) and os.path.isfile(wsig),
       "the witness pair lands content-named beside the log")
expect("hls-tlog-witness/v1" in open(wfile).read()
       and '"key_id"' in open(wfile).read(),
       "the witness names its schema and its signing key")

doc_bytes = open(wfile).read()
p2 = tlog("witness", w_log, "--sign", key, "--out", w_dir,
          "--trusted-comment", "timestamp:0",
          env=dict(os.environ, **PW_ENV))
expect(p2.returncode == 1 and "already exists" in p2.stderr,
       "witness refuses to overwrite a witness without --force")
p3 = tlog("witness", w_log, "--sign", key, "--out", w_dir,
          "--trusted-comment", "timestamp:0", "--force",
          env=dict(os.environ, **PW_ENV))
expect(p3.returncode == 0 and open(wfile).read() == doc_bytes,
       "re-witnessing the unchanged log re-writes the same bytes")

p, rep = tlog_json("witness-verify", wfile, "-p", pub,
                   "--against", w_log)
expect(p.returncode == 0 and rep["check"]["verdict"] == "byte-identical",
       "witness-verify against the same log: byte-identical")

grown = os.path.join(w_dir, "grown.json")
shutil.copyfile(w_log, grown)
make_log(grown, 2, name="wit", start=5, prev=head_w, append=True)
p, rep = tlog_json("witness-verify", wfile, "-p", pub, "--against", grown)
expect(p.returncode == 0 and rep["check"]["verdict"] == "held"
       and "prefix" in rep["check"]["detail"],
       "witness-verify against a grown log: held (the prefix rule)")

short = os.path.join(w_dir, "short.json")
make_log(short, 2, name="wit")
p, rep = tlog_json("witness-verify", wfile, "-p", pub, "--against", short)
expect(p.returncode == 1 and rep["check"]["verdict"] == "rollback"
       and "gone under a signed promise" in rep["check"]["detail"],
       "a log below a signed length: ROLLBACK, the evidence named")

# A well-formed fork: re-chain the tail after an evil seq 3.
wf_log = os.path.join(w_dir, "forked.json")
prev = com.GENESIS
out_recs = []
for i in range(1, 5):
    rec = {"kind": "publish" if i % 2 else "sign", "name": "wit",
           "version": "1.%d.0" % i, "prev_hash": prev, "seq": i,
           "timestamp": 1700000000 + i}
    if i == 3:
        rec["name"] = "evil"
    rec["chain_hash"] = com.chain_of(prev, rec)
    out_recs.append(rec)
    prev = rec["chain_hash"]
with open(wf_log, "w") as f:
    for r in out_recs:
        f.write(json.dumps(r, sort_keys=True) + "\n")
p, rep = tlog_json("witness-verify", wfile, "-p", pub, "--against", wf_log)
expect(p.returncode == 1 and rep["check"]["verdict"] == "rewritten",
       "a well-formed fork under the witness: rewritten")

tam_w = json.load(open(wfile))
tam_w["length"] = 99
write(wfile, json.dumps(tam_w, indent=2, sort_keys=True) + "\n")
p = tlog("witness-verify", wfile, "-p", pub, "--against", w_log)
expect(p.returncode == 1 and "does NOT verify" in p.stderr,
       "a tampered witness refuses (the signature covers the bytes)")
orig_doc = json.dumps({"length": 4, "head": head_w}, sort_keys=True)
p3r = tlog("witness", w_log, "--sign", key, "--out", w_dir,
           "--trusted-comment", "timestamp:0", "--force",
           env=dict(os.environ, **PW_ENV))
expect(p3r.returncode == 0, "the witness pair restored")

key2 = os.path.join(w_dir, "second.key")
pub2 = os.path.join(w_dir, "second.pub")
run(SIGN + ["keygen", "-f", key2, "-p", pub2, "--password-env", PW_VAR,
            "--iterations", "1000"], env=dict(os.environ, **PW_ENV))
p = tlog("witness-verify", wfile, "-p", pub2, "--against", w_log)
expect(p.returncode == 1 and "does NOT verify" in p.stderr,
       "another key refuses the witness (a trust anchor is not a vibe)")

nosig = os.path.join(w_dir, "naked.witness.json")
shutil.copyfile(wfile, nosig)
p = tlog("witness-verify", nosig, "-p", pub)
expect(p.returncode == 1 and "unsigned witness" in p.stderr,
       "a witness without its signature refuses")

broken = os.path.join(w_dir, "broken.json")
make_log(broken, 2, name="wit")
with open(broken, "a") as f:
    bad_rec = {"kind": "publish", "name": "x", "seq": 3,
               "timestamp": 1, "prev_hash": "1" * 64,
               "chain_hash": "2" * 64}
    f.write(json.dumps(bad_rec, sort_keys=True) + "\n")
p = tlog("witness", broken, "--sign", key, "--out", w_dir,
         env=dict(os.environ, **PW_ENV))
expect(p.returncode == 1 and "prev_hash" in p.stderr,
       "no witness over a broken chain (verify-before-sign)")

# ---------------------------------------------------------------------------
print("=== 6. the proofs (the record carried home) ===")
# ---------------------------------------------------------------------------

pr_dir = os.path.join(TMP, "proof")
os.makedirs(pr_dir)
pr_log = os.path.join(pr_dir, "p.json")
recs_p, head_p = make_log(pr_log, 5, name="prov")

p, rep = tlog_json("prove", "prov", "1.3.0", "--log", pr_log,
                   "--out", pr_dir)
expect(p.returncode == 0 and rep["proof"]["seq"] == 3,
       "prove by name+version finds the record at its seq")
proof_path = os.path.join(pr_dir, "hls-tlog-proof-prov-3.proof.json")
expect(os.path.isfile(proof_path),
       "the proof lands versioned under the tool and the seq")
proof_doc = json.load(open(proof_path))
expect(proof_doc["schema"] == com.PROOF_SCHEMA
       and proof_doc["length"] == 5 and proof_doc["head"] == head_p
       and len(proof_doc["tail"]) == 2,
       "the proof carries the record, the tail and the head")

p, rep = tlog_json("prove-verify", proof_path)
expect(p.returncode == 0 and rep["live"] is False
       and rep["verified"] is True,
       "prove-verify offline: the tail chains to the head")

p, rep = tlog_json("prove-verify", proof_path, "--log", pr_log)
expect(p.returncode == 0 and rep["live"] is True,
       "prove-verify --log: the record stands in the live history")

grown_p = os.path.join(pr_dir, "grown.json")
shutil.copyfile(pr_log, grown_p)
make_log(grown_p, 2, name="prov", start=6, prev=head_p, append=True)
p, rep = tlog_json("prove-verify", proof_path, "--log", grown_p)
expect(p.returncode == 0,
       "a proof stays valid over a grown log")

# Rewritten AT the record: an evil seq 3, tail re-chained after it.
rw_log = os.path.join(pr_dir, "rewritten.json")
prev = com.GENESIS
rw_recs = []
for i in range(1, 6):
    rec = {"kind": "publish" if i % 2 else "sign", "name": "prov",
           "version": "1.%d.0" % i, "prev_hash": prev, "seq": i,
           "timestamp": 1700000000 + i}
    if i == 3:
        rec["name"] = "evil"
    rec["chain_hash"] = com.chain_of(prev, rec)
    rw_recs.append(rec)
    prev = rec["chain_hash"]
with open(rw_log, "w") as f:
    for r in rw_recs:
        f.write(json.dumps(r, sort_keys=True) + "\n")
p, rep = tlog_json("prove-verify", proof_path, "--log", rw_log)
expect(p.returncode == 1 and "rewritten" in p.stdout,
       "a rewritten history kills the proof (the live check)")

tam_proof = json.load(open(proof_path))
tam_proof["record"] = dict(tam_proof["record"], name="evil")
tpath = os.path.join(pr_dir, "tampered.proof.json")
write(tpath, json.dumps(tam_proof, indent=2, sort_keys=True) + "\n")
p = tlog("prove-verify", tpath)
expect(p.returncode == 1 and "chain_hash" in p.stdout,
       "a tampered proof record refuses offline")

tam_tail = json.load(open(proof_path))
tam_tail["tail"] = [dict(tam_tail["tail"][0], kind="evil")] \
    + tam_tail["tail"][1:]
ttpath = os.path.join(pr_dir, "tamtail.proof.json")
write(ttpath, json.dumps(tam_tail, indent=2, sort_keys=True) + "\n")
p = tlog("prove-verify", ttpath)
expect(p.returncode == 1 and "chain" in p.stdout,
       "a tampered tail refuses offline")

garbage = os.path.join(pr_dir, "garbage.proof.json")
write(garbage, json.dumps({"schema": "hls-tlog-proof/v1",
                           "record": "not-a-record"}) + "\n")
p = tlog("prove-verify", garbage)
expect(p.returncode == 1 and "no record" in p.stdout,
       "a shapeless proof refuses")

p = tlog("prove", "absent", "--log", pr_log)
expect(p.returncode == 1 and "nothing to prove" in p.stderr,
       "proving an absent record refuses")
p, rep = tlog_json("prove", "--log", pr_log, "--seq", "4")
expect(p.returncode == 0 and rep["proof"]["seq"] == 4,
       "prove --seq finds the record by position")

# ---------------------------------------------------------------------------
print("=== 7. the ledger (one definition, end to end) ===")
# ---------------------------------------------------------------------------

rel_root = os.path.join(TMP, "rel_app")
write(os.path.join(rel_root, "hls-pkg.toml"),
      '[package]\nname = "tlog_app"\nversion = "1.0.7"\n\n'
      '[dependencies]\ntlog_dep = { path = "tests/_gate_s107_dep/main.hls" }'
      '\n\n[effects]\nallowed = ["IO", "Fs"]\n')
write(os.path.join(rel_root, "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')
write(os.path.join(DEP_DIR, "hls-pkg.toml"),
      '[package]\nname = "tlog_dep"\nversion = "0.1.0"\n')
write(os.path.join(DEP_DIR, "main.hls"),
      'fn sz() -> int uses Fs {\n    return fs_size("a")\n}\n')

had_log = os.path.exists(LOG_MAIN)
had_lock_file = os.path.exists(LOG_LOCK)
before = log_lines()
lp = run(PKG + ["lock"], cwd=rel_root)
expect(lp.returncode == 0, "hls-pkg lock pins the ledger fixture")
pp = run(PKG + ["publish"], cwd=rel_root)
expect(pp.returncode == 0, "hls-pkg publish records the same package")
expect(log_lines() == before + 2,
       "the fixture appended exactly the lock + publish records")

p, rep = tlog_json("verify")
expect(p.returncode == 0 and rep["sound"] is True and rep["records"] > 0,
       "hls-tlog verify replays the REPO ledger soundly")
vp = run(PKG + ["log", "--verify"])
expect(vp.returncode == 0,
       "hls-pkg log --verify agrees on the same file")
p, rep = tlog_json("summary")
head_summary = rep["view"]["head"]
with open(LOG_MAIN, "rb") as f:
    last = json.loads(f.readlines()[-1].decode("utf-8"))
expect(head_summary == last["chain_hash"],
       "the summary head IS the ledger's last chain hash")

copy_repo = os.path.join(TMP, "repo_copy.json")
shutil.copyfile(LOG_MAIN, copy_repo)
p, rep = tlog_json("gossip", LOG_MAIN, copy_repo)
expect(p.returncode == 0 and rep["verdict"] == "consensus",
       "gossip over the repo ledger and its copy: consensus")

p, rep = tlog_json("gossip", LOG_MAIN, "--require", "1")
expect(p.returncode == 0 and rep["verdict"] == "isolated",
       "a single ledger gossips isolated with --require 1")

p, rep = tlog_json("prove", "tlog_app", "1.0.7", "--out", TMP)
expect(p.returncode == 0
       and rep["proof"]["record"]["kind"] == "publish"
       and rep["proof"]["kind"] == "halis-tlog-inclusion",
       "the publish record proves by name from the live ledger")
proof_repo = os.path.join(
    TMP, "hls-tlog-proof-tlog_app-%d.proof.json"
         % rep["proof"]["seq"])
p, rep = tlog_json("prove-verify", proof_repo, "--log", LOG_MAIN)
expect(p.returncode == 0 and rep["live"] is True,
       "the repo proof verifies against the live ledger")

# Gate scratch hygiene: the log the fixtures wrote is not left behind
# for the commit.
if not had_log and os.path.exists(LOG_MAIN):
    try:
        os.unlink(LOG_MAIN)
    except OSError:
        pass
if not had_lock_file and os.path.exists(LOG_LOCK):
    try:
        os.unlink(LOG_LOCK)
    except OSError:
        pass
cache_dir = os.path.join(ROOT, ".hls-pkg-cache")
if os.path.isdir(cache_dir) and not os.listdir(cache_dir):
    os.rmdir(cache_dir)
shutil.rmtree(DEP_DIR, ignore_errors=True)

# ---------------------------------------------------------------------------
print("=== 8. the demos ===")
# ---------------------------------------------------------------------------

p1 = run([sys.executable, "boot/boot.py", DEMO])
expect(p1.returncode == 0 and "checks passed: 6" in p1.stdout,
       "the demo runs (interpreter)")
p2 = run([sys.executable, "boot/boot.py", OK_TEST])
expect(p2.returncode == 0 and "checks passed: 5" in p2.stdout,
       "the ok-test runs (interpreter)")

hlc = os.path.join(ROOT, "bin", "hlc")
if os.path.exists(hlc):
    c1 = os.path.join(TMP, "demo_native.c")
    c2 = os.path.join(TMP, "ok_native.c")
    b1 = os.path.join(TMP, "demo_native")
    b2 = os.path.join(TMP, "ok_native")
    ok1 = run([hlc, DEMO, c1]).returncode == 0 and \
        run(["cc", "-O2", "-o", b1, c1, "-lm", "-pthread"]).returncode == 0
    ok2 = run([hlc, OK_TEST, c2]).returncode == 0 and \
        run(["cc", "-O2", "-o", b2, c2, "-lm", "-pthread"]).returncode == 0
    if ok1 and ok2:
        n1 = run([b1])
        expect(n1.returncode == 0 and n1.stdout == p1.stdout,
               "the demo agrees byte for byte with the native binary")
        n2 = run([b2])
        expect(n2.returncode == 0 and n2.stdout == p2.stdout,
               "the ok-test agrees byte for byte with the native binary")
    else:
        bad("native compilation of the demos failed")
else:
    print("  [note] bin/hlc not built — the native half of section 8 "
          "is skipped (the gate stays hermetic)")

# ---------------------------------------------------------------------------
print("=== 9. the tools ===")
# ---------------------------------------------------------------------------

for f in (DEMO, OK_TEST):
    with open(f) as fh:
        original = fh.read()
    p1f = os.path.join(TMP, "fmt1.hls")
    p2f = os.path.join(TMP, "fmt2.hls")
    with open(p1f, "w") as fh:
        fh.write(original)
    run([sys.executable, "tools/hlfmt.py", "-w", p1f])
    shutil.copyfile(p1f, p2f)
    run([sys.executable, "tools/hlfmt.py", "-w", p2f])
    with open(p1f) as fh:
        once = fh.read()
    with open(p2f) as fh:
        twice = fh.read()
    expect(once == twice, "hlfmt: stable formatting on %s" % f)

lints = [run([sys.executable, "tools/hllint.py", f]) for f in (DEMO, OK_TEST)]
if all(l.returncode == 0 for l in lints):
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings on the new sources")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 107 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
