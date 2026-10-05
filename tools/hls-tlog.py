#!/usr/bin/env python3
"""hls-tlog — Stage 107 transparency log gossip (multi-source verify).

Usage:
  python3 tools/hls-tlog.py verify [LOG] [--json]
  python3 tools/hls-tlog.py summary [LOG] [--json]
  python3 tools/hls-tlog.py gossip SOURCE... [--timeout S] [--max-bytes N]
                             [--max-lag N] [--require N] [--json]
  python3 tools/hls-tlog.py witness [LOG] --sign SECRET
                             [--password-env VAR] [--out DIR]
                             [--trusted-comment STR] [--force] [--json]
  python3 tools/hls-tlog.py witness-verify WITNESS [-p PUB | -P HEX]
                             [--against LOG] [--json]
  python3 tools/hls-tlog.py prove NAME [VERSION] [--log LOG] [--seq N]
                             [--out DIR] [--json]
  python3 tools/hls-tlog.py prove-verify PROOF [--log LOG] [--json]
  python3 tools/hls-tlog.py selftest

Every claim the toolchain writes — publish, the SBOM, repro, sign —
chains into ONE local ledger, and until now that ledger had exactly
one witness: itself. A single witness is a single point of failure.
Stage 107 gives the ledger the Certificate Transparency property that
makes a log trustworthy despite its operator: EVERYONE KEEPS A VIEW,
AND VIEWS ARE COMPARED. A copy of the log in a teammate's checkout, a
mirror on a CI cache, a URL in another repo — `hls-tlog gossip` asks
them all, classifies every pair (agree / lag / fork / diverged), and
turns any disagreement into evidence with the pair named.

Views are claims, so they get names: `witness` signs a view with the
Stage 106 Ed25519 keys (the minisign discipline, the same two-
signature files — no new crypto, one keypair for the whole supply
chain). A witness is what makes ROLLBACK visible: a mirror missing
the newest records is a lagging mirror; a mirror missing records
SOMEONE SIGNED is evidence. `witness-verify` draws exactly that line
against the current log.

`prove` carries one record home: the record, its tail, the head they
chain to — verifiable offline for coherence, and against the live log
for truth (a proof stays valid over a GROWN log, dies on a rewritten
one).

The chain arithmetic is ONE definition: sha256(prev_hash +
canonical-json(record minus chain_hash)) — byte-identical to
hpkg_log's writer since Stage 13. The gate runs `hls-pkg log --verify`
and `hls-tlog verify` on the same file and refuses if they disagree.

Exit contract: 0 verified / consensus / held; 1 on any refusal (a
fork, a rollback, a rewritten history, a broken chain, a bad
signature, an insufficient source count); 2 for usage errors. Gossip,
verify, witness-verify and prove-verify are read-only end to end.
--json prints the machine report (schema names in every report).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from hltlog_parts import hltlog_chain as chain_mod            # noqa: E402
from hltlog_parts import hltlog_common as com                 # noqa: E402
from hltlog_parts import hltlog_selftest as st                # noqa: E402
from hltlog_parts import hltlog_witness as wit                # noqa: E402
from hltlog_parts import hltlog_sources as src                # noqa: E402
from hlsign_parts import hlsign_format as fmt                 # noqa: E402
from hlaudit_parts import hal_ops                             # noqa: E402

TOOL = com.TOOL


class CliError(Exception):
    """A refusal the CLI reports on stderr with exit 1."""


def _password(args, what):
    """The passphrase, in hls-sign's order: the variable --password-env
    names; then HLS_SIGN_PASSWORD; then a prompt when a terminal is
    attached; then empty. A CI run never hangs on a hidden prompt."""
    var = getattr(args, "password_env", None)
    if var:
        val = os.environ.get(var)
        if val is None:
            raise CliError("the environment variable %s is not set — it "
                           "should hold the %s passphrase" % (var, what))
        return val
    val = os.environ.get("HLS_SIGN_PASSWORD")
    if val is not None:
        return val
    if sys.stdin is not None and sys.stdin.isatty():
        import getpass
        return getpass.getpass("passphrase for %s (empty ok): " % what)
    return ""


def _load_log(path):
    """The log, parsed AND replayed — nothing downstream (summary,
    witness, prove, gossip) may reason over a chain this tool has not
    verified. Returns (records, head, log_sha256)."""
    data = com.read_log_bytes(path)
    records = com.parse_log(data, path)
    head, errors = chain_mod.verify_chain(records, path)
    if errors:
        where, msg = errors[0]
        more = "" if len(errors) == 1 \
            else " (and %d more break(s))" % (len(errors) - 1)
        raise CliError("%s: %s%s — run `hls-tlog verify` for the full "
                       "report" % (where, msg, more))
    return records, head, com.sha256_bytes(data)


def _resolve_public(args):
    """The trust anchor — hls-sign's rule: -p file or -P raw hex, and
    verification never falls back to a key it was not handed."""
    if getattr(args, "raw_pub", None):
        try:
            raw = bytes.fromhex(args.raw_pub)
        except ValueError:
            raise CliError("-P wants 64 hex characters (a raw Ed25519 "
                           "public key)")
        if len(raw) != 32:
            raise CliError("-P wants 64 hex characters (a raw Ed25519 "
                           "public key)")
        return raw, "hex"
    pub_path = getattr(args, "pub", None)
    if not pub_path:
        pub_path = os.environ.get("HLS_SIGN_PUB", fmt.DEFAULT_PUB_NAME)
    if not os.path.isfile(pub_path):
        raise CliError("no public key at %s — pass -p FILE or -P HEX "
                       "(verification verifies AGAINST something)"
                       % pub_path)
    public, _, _ = fmt.read_public(pub_path)
    return public, pub_path


def _safe_name(name):
    return "".join(c if (c.isalnum() or c in "._-") else "_"
                   for c in name) or "unnamed"


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_verify(args):
    path = args.log or com.TRANSPARENCY_LOG
    data = com.read_log_bytes(path)
    records = com.parse_log(data, path)
    head, errors = chain_mod.verify_chain(records, path)
    if args.json:
        print(json.dumps({
            "schema": com.VERIFY_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "log": path, "records": len(records), "head": head,
            "sound": not errors,
            "errors": [{"where": w, "error": m} for w, m in errors[:50]],
            "error_count": len(errors),
        }, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print("== hls-tlog verify ==")
    print("  log:     %s" % path)
    print("  records: %d" % len(records))
    print("  head:    %s" % head)
    if not errors:
        print("  the chain is sound: every record chains from the one "
              "before it, seq 1..%d with no gaps" % len(records))
        return 0
    print("  BROKEN — %d error(s):" % len(errors))
    for where, msg in errors[:10]:
        print("    %s: %s" % (where, msg))
    if len(errors) > 10:
        print("    ... and %d more" % (len(errors) - 10))
    return 1


def cmd_summary(args):
    path = args.log or com.TRANSPARENCY_LOG
    records, head, log_sha = _load_log(path)
    view = com.compute_view(records, log_sha)
    if args.json:
        print(json.dumps({
            "schema": com.VIEW_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "log": path, "sound": True, "view": view,
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-tlog summary ==")
    print("  log:     %s (chain verified)" % path)
    print("  length:  %d" % view["length"])
    print("  head:    %s" % view["head"])
    print("  bytes:   sha256 %s" % view["log_sha256"])
    kinds = ", ".join("%s x%d" % (k, v)
                      for k, v in view["kinds"].items()) or "(none)"
    print("  kinds:   %s" % kinds)
    return 0


def cmd_gossip(args):
    if not args.sources:
        sys.stderr.write("%s: gossip needs at least one source\n" % TOOL)
        return 2
    sources = []
    for s in args.sources:
        label, data, reason = src.fetch(s, timeout=args.timeout,
                                        max_bytes=args.max_bytes)
        if reason is not None:
            sources.append({"source": label, "ok": False,
                            "error": reason})
            continue
        try:
            records = com.parse_log(data, label)
            head, errors = chain_mod.verify_chain(records, label)
            if errors:
                sources.append({"source": label, "ok": False,
                                "error": "the chain is broken (%d "
                                         "error(s))" % len(errors)})
                continue
            view = com.compute_view(records, com.sha256_bytes(data))
            heads = {r["seq"]: r["chain_hash"] for r in records}
            sources.append({"source": label, "ok": True,
                            "view": view, "heads": heads})
        except com.TlogError as ex:
            sources.append({"source": label, "ok": False,
                            "error": str(ex)})

    oks = [s for s in sources if s["ok"]]
    result = chain_mod.gossip_verdict(
        [{"source": s["source"], "length": s["view"]["length"],
          "head": s["view"]["head"]} for s in oks],
        [s["heads"] for s in oks])
    stale = (args.max_lag is not None
             and result["max_lag"] > args.max_lag)
    if len(oks) < args.require:
        # Not enough conversation for the demanded plurality.
        result["verdict"] = "insufficient"
    elif len(oks) == 1:
        # --require 1 explicitly allowed a single source: nobody to
        # compare against, no fork evidence either way.
        result["verdict"] = "isolated"
    elif result["verdict"] != "fork" and stale:
        # Evidence outranks health: a fork is never downgraded to a
        # lag complaint.
        result["verdict"] = "stale"

    if args.json:
        print(json.dumps({
            "schema": com.GOSSIP_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "sources": [{"source": s["source"], "ok": s["ok"],
                         **({"view": s["view"]} if s["ok"] else
                            {"error": s["error"]})}
                        for s in sources],
            "ok_sources": len(oks),
            "verdict": result["verdict"],
            "head": result["head"], "max_lag": result["max_lag"],
            "pairs": result["pairs"],
            "require": args.require, "max_lag_limit": args.max_lag,
        }, indent=2, sort_keys=True))
        return 0 if result["verdict"] in ("consensus", "isolated") else 1

    print("== hls-tlog gossip ==")
    print("  sources: %d asked, %d answered" % (len(sources), len(oks)))
    for s in sources:
        if s["ok"]:
            v = s["view"]
            print("    ok      %s  (length %d, head %s...)"
                  % (s["source"], v["length"], v["head"][:16]))
        else:
            print("    refused %s  (%s)" % (s["source"], s["error"]))
    for pair in result["pairs"]:
        if pair["class"] in ("fork", "diverged"):
            print("  CONFLICT %s vs %s: %s"
                  % (pair["a"], pair["b"], pair["class"]))
        elif pair["class"] == "lag" and pair.get("lag"):
            print("    lag    %s vs %s: %d record(s) behind"
                  % (pair["a"], pair["b"], pair["lag"]))
    if result["verdict"] == "fork":
        print("  head:    none — the sources disagree about the "
              "history")
        print("  verdict: FORK — the sources disagree about the "
              "history; a quorum cannot outvote the evidence")
        return 1
    print("  head:    %s (the longest answered view)" % result["head"])
    if result["verdict"] == "consensus":
        print("  verdict: consensus — every answered source holds the "
              "same history (max lag %d)" % result["max_lag"])
        return 0
    if result["verdict"] == "stale":
        print("  verdict: stale — lag %d exceeds the %d record limit"
              % (result["max_lag"], args.max_lag))
        return 1
    if result["verdict"] == "isolated":
        print("  verdict: isolated — one source answered (--require 1 "
              "allowed it); nothing was compared, nothing was proven")
        return 0
    print("  verdict: insufficient — %d of %d required source(s) "
          "answered; gossip alone is not gossip"
          % (len(oks), args.require))
    return 1


def cmd_witness(args):
    if not args.sign:
        sys.stderr.write("%s: witness needs --sign SECRET (the Stage 106 "
                         "key signs the view)\n" % TOOL)
        return 2
    if not os.path.isfile(args.sign):
        raise CliError("no secret key at %s — run `hls-sign keygen` "
                       "first" % args.sign)
    password = _password(args, "the secret key")
    seed, public, _kid, _c = fmt.read_secret(args.sign, password)
    path = args.log or com.TRANSPARENCY_LOG
    records, _head, log_sha = _load_log(path)
    view = com.compute_view(records, log_sha)
    witness = wit.make_witness(view, public)
    trusted = args.trusted_comment or fmt.default_trusted_comment(
        os.path.basename(path))
    out_dir = args.out or os.path.dirname(os.path.abspath(path))
    doc_path, sig_path = wit.write_witness(witness, seed, public, out_dir,
                                           trusted, force=args.force)
    # Stage 112: minting a signed view is a privileged op — the
    # witness promises the log will never shrink below this head.
    hal_ops.record(hal_ops.TLOG_WITNESS, "ok", subject=path,
                   detail={"length": view["length"],
                           "head": view["head"],
                           "key_id": witness["key_id"],
                           "witness": doc_path})
    if args.json:
        print(json.dumps({
            "schema": com.WITNESS_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "witness": witness,
            "written": [doc_path, sig_path],
            "trusted_comment": trusted,
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-tlog witness ==")
    print("  log:     %s (chain verified, %d record(s))"
          % (path, view["length"]))
    print("  view:    length %d, head %s..." % (view["length"],
                                                view["head"][:16]))
    print("  key:     %s" % witness["key_id"])
    print("  wrote:   %s" % doc_path)
    print("  wrote:   %s" % sig_path)
    print("  a signed view is a promise: if this log ever shrinks "
          "below seq %d, every holder has evidence" % view["length"])
    return 0


def cmd_witness_verify(args):
    public, anchor = _resolve_public(args)
    doc, trusted = wit.read_witness(args.witness)
    if not wit.verify_witness_signature(args.witness, public):
        raise CliError("the witness signature does NOT verify against "
                       "this key — the view was tampered with or signed "
                       "by another key")
    ts = wit.trusted_timestamp(trusted)
    result = None
    if getattr(args, "against", None):
        records, _head, log_sha = _load_log(args.against)
        result = wit.check_against_log(doc, records, log_sha)
    if args.json:
        print(json.dumps({
            "schema": com.WITNESS_VERIFY_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "witness": doc, "anchor": anchor,
            "signed_timestamp": ts,
            "trusted_comment": trusted,
            **({"check": result} if result else {}),
            "verified": True,
        }, indent=2, sort_keys=True))
        if result is not None and result["verdict"] in ("rollback",
                                                        "rewritten"):
            return 1
        return 0
    print("== hls-tlog witness-verify ==")
    print("  witness: %s (signature verified, key %s)"
          % (args.witness, doc["key_id"]))
    print("  view:    length %d, head %s..." % (doc["length"],
                                                doc["head"][:16]))
    if ts is not None:
        print("  signed:  at unix second %d (from the signed comment)"
              % ts)
    if result is not None:
        print("  against: %s" % args.against)
        print("  check:   %s — %s" % (result["verdict"],
                                      result["detail"]))
        if result["verdict"] in ("held", "byte-identical"):
            return 0
        return 1
    return 0


def _find_record(records, name, version, seq):
    """The LAST matching record wins (the log is append-only; the
    newest claim is the live one)."""
    found = None
    for rec in records:
        if seq is not None:
            if rec.get("seq") == seq:
                found = rec
            continue
        if name is not None and rec.get("name") != name:
            continue
        if version is not None and rec.get("version") != version:
            continue
        if name is None and version is None:
            continue
        found = rec
    return found


def cmd_prove(args):
    if args.seq is None and args.name is None:
        sys.stderr.write("%s: prove needs a NAME (and optionally a "
                         "VERSION) or --seq N\n" % TOOL)
        return 2
    path = args.log or com.TRANSPARENCY_LOG
    records, head, _sha = _load_log(path)
    rec = _find_record(records, args.name, args.version, args.seq)
    if rec is None:
        raise CliError("no record matches %s in %s — nothing to prove"
                       % (args.name or ("seq %d" % args.seq), path))
    index = records.index(rec)
    proof = chain_mod.build_proof(records, index)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        stem = "hls-tlog-proof-%s-%d" % (_safe_name(rec.get("name")
                                                    or "record"),
                                         rec["seq"])
        out_path = os.path.join(args.out, stem + com.PROOF_SUFFIX)
        with open(out_path, "wb") as f:
            f.write(com.canon_bytes(proof))
    else:
        out_path = None
    if args.json:
        print(json.dumps({
            "schema": com.PROOF_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "proof": proof,
            **({"written": out_path} if out_path else {}),
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-tlog prove ==")
    print("  log:     %s (chain verified)" % path)
    print("  record:  seq %d, kind %s%s"
          % (rec["seq"], rec.get("kind") or "untyped",
             ", name %s" % rec["name"] if rec.get("name") else ""))
    print("  head:    %s (the tail chains there: %d record(s) after)"
          % (head[:16] + "...", len(proof["tail"])))
    if out_path:
        print("  wrote:   %s" % out_path)
    print("  the proof verifies offline for coherence; prove-verify "
          "--log checks it against the live history")
    return 0


def cmd_prove_verify(args):
    if not os.path.isfile(args.proof):
        raise CliError("no proof at %s" % args.proof)
    try:
        proof = json.load(open(args.proof, encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise CliError("%s is not JSON (%s)" % (args.proof, ex))
    if not isinstance(proof, dict) or proof.get("schema") != com.PROOF_SCHEMA:
        raise CliError("%s is not a %s document"
                       % (args.proof, com.PROOF_SCHEMA))
    errors = chain_mod.verify_proof_offline(proof)
    live_checked = False
    if not errors and getattr(args, "log", None):
        records, _head, _sha = _load_log(args.log)
        errors = chain_mod.verify_proof_live(proof, records)
        live_checked = True
    if args.json:
        print(json.dumps({
            "schema": com.PROOF_VERIFY_SCHEMA,
            "tool": TOOL, "tool_version": com.VERSION,
            "proof": args.proof,
            "seq": proof.get("seq"),
            "live": live_checked,
            "verified": not errors,
            "errors": [{"where": w, "error": m} for w, m in errors],
        }, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print("== hls-tlog prove-verify ==")
    print("  proof:   %s (seq %s)" % (args.proof, proof.get("seq")))
    if live_checked:
        print("  live:    checked against %s" % args.log)
    else:
        print("  offline: the record and its tail chain coherently to "
              "the head (pass --log to check the live history)")
    if errors:
        print("  REFUSED — %d error(s):" % len(errors))
        for where, msg in errors[:10]:
            print("    %s: %s" % (where, msg))
        return 1
    print("  verified: the record stands in a history that chains to "
          "the head")
    return 0


def cmd_selftest(_args):
    print("== hls-tlog selftest ==")
    for n in st.selftest():
        print("  ok: %s" % n)
    print("  the chain arithmetic is hpkg_log's writer, pinned to a "
          "hand-built canonical string")
    return 0


# ---------------------------------------------------------------------------
# CLI wiring.
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL, description="Stage 107 transparency log gossip "
                               "(multi-source verify).")
    sub = ap.add_subparsers(dest="cmd")

    vf = sub.add_parser("verify", help="replay a log's hash chain")
    vf.add_argument("log", nargs="?", default=None,
                    help="the log file (default the repo ledger)")
    vf.add_argument("--json", action="store_true")
    vf.set_defaults(fn=cmd_verify)

    sm = sub.add_parser("summary", help="the view of a verified log")
    sm.add_argument("log", nargs="?", default=None)
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(fn=cmd_summary)

    gp = sub.add_parser("gossip", help="compare views across sources "
                                       "(files, dirs, http URLs)")
    gp.add_argument("sources", nargs="*",
                    help="log files, directories (their ledger), or "
                         "http(s) URLs")
    gp.add_argument("--timeout", type=float, default=com.DEFAULT_TIMEOUT,
                    help="per-HTTP-source seconds (default %(default)s)")
    gp.add_argument("--max-bytes", type=int,
                    default=com.DEFAULT_MAX_BYTES,
                    help="per-source size cap (default %(default)s)")
    gp.add_argument("--max-lag", type=int, default=None,
                    help="fail when the worst lag exceeds N records "
                         "(default: lag never fails)")
    gp.add_argument("--require", type=int, default=2,
                    help="ok sources required (default 2)")
    gp.add_argument("--json", action="store_true")
    gp.set_defaults(fn=cmd_gossip)

    wt = sub.add_parser("witness", help="sign a log's view (the Stage "
                                        "106 keys)")
    wt.add_argument("log", nargs="?", default=None)
    wt.add_argument("--sign", default=None, metavar="SECRET",
                    help="the hls-sign secret key")
    wt.add_argument("--password-env", default=None, metavar="VAR")
    wt.add_argument("--out", default=None, metavar="DIR",
                    help="where the witness pair lands (default beside "
                         "the log)")
    wt.add_argument("--trusted-comment", default=None)
    wt.add_argument("--force", action="store_true")
    wt.add_argument("--json", action="store_true")
    wt.set_defaults(fn=cmd_witness, audit_op=hal_ops.TLOG_WITNESS)

    wv = sub.add_parser("witness-verify",
                        help="verify a signed view; --against checks "
                             "the current log")
    wv.add_argument("witness", help="the witness .witness.json")
    wv.add_argument("-p", "--pub", default=None)
    wv.add_argument("-P", "--raw-pub", default=None, metavar="HEX")
    wv.add_argument("--against", default=None, metavar="LOG")
    wv.add_argument("--json", action="store_true")
    wv.set_defaults(fn=cmd_witness_verify)

    pr = sub.add_parser("prove", help="an inclusion proof for one "
                                      "record")
    pr.add_argument("name", nargs="?", default=None,
                    help="the record's name field")
    pr.add_argument("version", nargs="?", default=None)
    pr.add_argument("--log", default=None)
    pr.add_argument("--seq", type=int, default=None,
                    help="prove by position instead of name")
    pr.add_argument("--out", default=None, metavar="DIR")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(fn=cmd_prove)

    pv = sub.add_parser("prove-verify",
                        help="verify a proof (offline, or --log for "
                             "the live history)")
    pv.add_argument("proof", help="the proof .proof.json")
    pv.add_argument("--log", default=None)
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(fn=cmd_prove_verify)

    stt = sub.add_parser("selftest", help="the stage's pinned numbers")
    stt.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_usage(sys.stderr)
        sys.stderr.write("error: a command is required (verify, "
                         "summary, gossip, witness, witness-verify, "
                         "prove, prove-verify, selftest)\n")
        return 2
    try:
        return args.fn(args)
    except (CliError, com.TlogError, fmt.SignError) as ex:
        sys.stderr.write("%s: %s\n" % (TOOL, ex))
        # Stage 112: a refused witness is recorded too — the same rule
        # every hooked tool runs; read-only commands record nothing.
        op = getattr(args, "audit_op", None)
        if op:
            hal_ops.record_refused(op, str(ex))
        return 1


if __name__ == "__main__":
    sys.exit(main())
