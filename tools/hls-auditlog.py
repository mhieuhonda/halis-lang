#!/usr/bin/env python3
"""hls-auditlog — Stage 112 audit-log signing (every privileged op
hashed + chained).

Usage:
  python3 tools/hls-auditlog.py list [LOG] [--op OP] [--outcome ok|refused]
                                   [--actor STR] [--json]
  python3 tools/hls-auditlog.py show SEQ [LOG] [--json]
  python3 tools/hls-auditlog.py verify [LOG] [--json]
  python3 tools/hls-auditlog.py sign [LOG] [-s SECRET] [--out DIR]
                              [--password-env VAR] [--trusted-comment STR]
                              [--force] [--json]
  python3 tools/hls-auditlog.py verify-sign CKPT [-p PUB | -P HEX]
                              [--against LOG] [--json]
  python3 tools/hls-auditlog.py selftest

The transparency ledger records what was CLAIMED about packages;
this tool's log records what was DONE, by whom, and what was tried
and refused. Every privileged operation the toolchain performs —
locking a dependency tree, adding a dependency, publishing, minting
a signing key, signing a payload or a release, arming the sandbox
around a command, pinning a sandbox posture, witnessing a
transparency view, checkpointing this very log — appends one
hash-chained entry: the actor (user@host), the op (a closed
inventory), the subject, the outcome (ok / refused), and the small
detail the op owes the record. Refused attempts get the SAME entry
as successes: an audit gate that said no is the most interesting
fact in the file.

The chain arithmetic is the family's ONE definition —
sha256(prev_hash + canonical-json(entry minus chain_hash)) —
imported from the same module hls-tlog pinned in Stage 107, never
re-implemented. Signing is the Stage 106 discipline: `sign` replays
the chain first (nothing signs a log it has not verified), then
pins (length, head, log_sha256, the ops/outcomes/actors inventory)
in an Ed25519 checkpoint document and its detached minisign
signature, then appends the checkpoint op to the log itself — the
log describes its own checkpointing. `verify-sign` draws the
witness verdicts: held / byte-identical / rewritten / rollback.

Exit contract: 0 verified / signed / listed; 1 on any refusal (a
broken chain, a bad signature, a rollback, a rewritten history, a
missing key); 2 for usage errors. list, show, verify and verify-sign
are read-only end to end. --json prints the machine report (schema
names in every report). The log path follows HLS_AUDIT_LOG (default
`<repo>/.hls-audit.log`); strict write mode follows HLS_AUDIT_STRICT.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from hlaudit_parts import hal_log                    # noqa: E402
from hlaudit_parts import hal_ops                    # noqa: E402
from hlaudit_parts import hal_checkpoint as ckpt     # noqa: E402
from hlaudit_parts.hal_common import (               # noqa: E402
    AuditlogError, CHECKPOINT_VERIFY_SCHEMA, LIST_SCHEMA, OUTCOMES,
    SHOW_SCHEMA, SIGN_SCHEMA, TOOL, VERIFY_SCHEMA, VERSION,
    audit_log_path,
)
from hlsign_parts import hlsign_format as fmt        # noqa: E402


class CliError(Exception):
    """A refusal the CLI reports on stderr with exit 1."""


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _password(args, what):
    """The passphrase, in hls-sign's order: --password-env's variable;
    then HLS_SIGN_PASSWORD; then a prompt when a terminal is
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


def _load_secret(args):
    secret_path = getattr(args, "key", None) or getattr(args, "secret", None)
    if not secret_path:
        secret_path = os.environ.get("HLS_SIGN_KEY", fmt.DEFAULT_SECRET_NAME)
    if not os.path.isfile(secret_path):
        raise CliError("no secret key at %s — run `hls-sign keygen` (or "
                       "pass -s/--key)" % secret_path)
    password = _password(args, "the secret key")
    seed, public, _, _ = fmt.read_secret(secret_path, password)
    return seed, public, secret_path


def _resolve_public(args):
    """The trust anchor — hls-sign's rule: -p file or -P raw hex;
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


def _load_and_verify(path):
    """The log, parsed AND replayed — nothing downstream (list, show,
    sign) reasons over a chain this tool has not verified. Returns
    (records, head, log_sha256)."""
    data = hal_log.read_log_bytes(path)
    try:
        records = hal_log.parse_log(data, path)
    except AuditlogError as ex:
        raise CliError(str(ex))
    head, errors = hal_log.verify_log(records, path)
    if errors:
        where, msg = errors[0]
        more = "" if len(errors) == 1 \
            else " (and %d more break(s))" % (len(errors) - 1)
        raise CliError("%s: %s%s — run `hls-auditlog verify` for the "
                       "full report" % (where, msg, more))
    return records, head, ckpt.log_sha256_of(path)


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_list(args):
    path = args.log or audit_log_path()
    records, head, log_sha = _load_and_verify(path)
    shown = records
    if args.op:
        shown = [r for r in shown if r.get("op") == args.op]
    if args.outcome:
        if args.outcome not in OUTCOMES:
            raise CliError("--outcome is %s — not %r"
                           % ("/".join(OUTCOMES), args.outcome))
        shown = [r for r in shown if r.get("outcome") == args.outcome]
    if args.actor:
        shown = [r for r in shown if r.get("actor") == args.actor]
    if args.json:
        inv = hal_log.summarize(shown)
        print(json.dumps({
            "schema": LIST_SCHEMA,
            "tool": TOOL, "tool_version": VERSION,
            "log": path, "length": len(records),
            "head": head, "log_sha256": log_sha,
            "shown": len(shown),
            "ops": inv["ops"], "outcomes": inv["outcomes"],
            "actors": inv["actors"],
            "entries": shown,
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-auditlog list ==")
    print("  log:     %s (%d entr%s, head %s...)"
          % (path, len(records), "y" if len(records) == 1 else "ies",
             head[:16]))
    if shown:
        print("  %4s  %-19s  %-22s  %-8s  %s"
              % ("seq", "time (utc)", "op", "outcome", "actor / subject"))
        print("  " + "-" * 96)
        for r in shown:
            ts = datetime.datetime.fromtimestamp(
                r["timestamp"], datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S")
            subj = r.get("subject") or ""
            who = "%s / %s" % (r.get("actor"), subj) \
                if subj else str(r.get("actor"))
            print("  %4d  %-19s  %-22s  %-8s  %s"
                  % (r["seq"], ts, r.get("op"), r.get("outcome"), who))
    else:
        print("  (no entries match)")
    return 0


def cmd_show(args):
    path = args.log or audit_log_path()
    records, head, _log_sha = _load_and_verify(path)
    rec = next((r for r in records if r.get("seq") == args.seq), None)
    if rec is None:
        raise CliError("no entry with seq %d in %s (%d entr%s)"
                       % (args.seq, path, len(records),
                          "y" if len(records) == 1 else "ies"))
    if args.json:
        print(json.dumps({
            "schema": SHOW_SCHEMA,
            "tool": TOOL, "tool_version": VERSION,
            "log": path, "entry": rec,
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-auditlog show ==")
    print("  seq:      %d" % rec["seq"])
    print("  time:     %s (unix %d)"
          % (datetime.datetime.fromtimestamp(
              rec["timestamp"], datetime.timezone.utc).strftime(
              "%Y-%m-%d %H:%M:%S UTC"), rec["timestamp"]))
    print("  op:       %s" % rec["op"])
    print("  actor:    %s" % rec["actor"])
    print("  subject:  %s" % (rec.get("subject") or "<none>"))
    print("  outcome:  %s" % rec["outcome"])
    print("  tool:     %s %s" % (rec.get("tool"), rec.get("tool_version")))
    if rec.get("detail"):
        print("  detail:")
        for k in sorted(rec["detail"]):
            print("    %s: %s" % (k, rec["detail"][k]))
    print("  chain:    prev %s..." % rec["prev_hash"][:16])
    print("            this %s..." % rec["chain_hash"][:16])
    return 0


def cmd_verify(args):
    path = args.log or audit_log_path()
    data = hal_log.read_log_bytes(path)
    try:
        records = hal_log.parse_log(data, path)
    except AuditlogError as ex:
        raise CliError(str(ex))
    head, errors = hal_log.verify_log(records, path)
    inv = hal_log.summarize(records)
    report = {
        "schema": VERIFY_SCHEMA,
        "tool": TOOL, "tool_version": VERSION,
        "log": path,
        "length": len(records),
        "head": head,
        "log_sha256": ckpt.log_sha256_of(path),
        "ops": inv["ops"],
        "outcomes": inv["outcomes"],
        "actors": inv["actors"],
        "first_timestamp": inv["first_timestamp"],
        "last_timestamp": inv["last_timestamp"],
        "sound": not errors,
        "errors": [{"where": w, "error": m} for w, m in errors],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print("== hls-auditlog verify ==")
    print("  log:      %s" % path)
    print("  entries:  %d (head %s...)" % (len(records), head[:16]))
    if inv["ops"]:
        print("  ops:      %s" % ", ".join(
            "%s x%d" % (k, v) for k, v in inv["ops"].items()))
        print("  outcomes: %s" % ", ".join(
            "%s x%d" % (k, v) for k, v in inv["outcomes"].items()))
        print("  actors:   %s" % ", ".join(
            "%s x%d" % (k, v) for k, v in inv["actors"].items()))
    if errors:
        print("  the chain is BROKEN — the log is evidence now:")
        for w, m in errors:
            print("    %s: %s" % (w, m))
        return 1
    print("  the chain is sound: every entry hashes to its place, "
          "from genesis to head")
    return 0


def cmd_sign(args):
    """Verify, pin, sign, record. The checkpoint covers the log AS IT
    WAS when signed; the checkpoint op's own entry lands at seq
    length+1 — inside the NEXT checkpoint's reach, not this one's."""
    path = args.log or audit_log_path()
    records, head, log_sha = _load_and_verify(path)
    seed, public, secret_path = _load_secret(args)
    keyid = fmt.key_id_hex(public)
    document = ckpt.build_checkpoint(records, log_sha, keyid)
    out_dir = os.path.realpath(os.path.abspath(args.out or os.getcwd()))
    if not os.path.isdir(out_dir):
        raise CliError("output directory does not exist: %s" % out_dir)
    trusted = args.trusted_comment or fmt.default_trusted_comment(
        ckpt.checkpoint_base(document) + ckpt.CHECKPOINT_SUFFIX)
    doc_path, sig_path = ckpt.write_checkpoint(
        document, seed, public, out_dir, trusted, force=args.force)

    detail = {
        "length": document["length"],
        "head": document["head"],
        "log_sha256": log_sha,
        "key_id": keyid,
        "ckpt_sha256": _sha256_file(doc_path),
        "sig_sha256": _sha256_file(sig_path),
        "ckpt": doc_path,
    }
    rec = hal_log.append_entry(path, {
        "kind": "op", "op": hal_ops.AUDIT_CHECKPOINT,
        "actor": hal_ops.current_actor(), "outcome": "ok",
        "tool": TOOL, "tool_version": VERSION,
        "subject": "%s@%d" % (keyid[:16], document["length"]),
        "detail": detail,
    })

    if args.json:
        print(json.dumps({
            "schema": SIGN_SCHEMA,
            "tool": TOOL, "tool_version": VERSION,
            "log": path, "checkpoint": document,
            "written": [doc_path, sig_path],
            "ckpt_sha256": detail["ckpt_sha256"],
            "sig_sha256": detail["sig_sha256"],
            "record_seq": rec["seq"],
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-auditlog sign ==")
    print("  log:        %s (%d entr%s, head %s...)"
          % (path, document["length"],
             "y" if document["length"] == 1 else "ies",
             document["head"][:16]))
    print("  key:        %s" % keyid)
    print("  wrote:      %s" % doc_path)
    print("  wrote:      %s" % sig_path)
    print("  checkpoint op recorded at seq %d (chain %s...)"
          % (rec["seq"], rec["chain_hash"][:16]))
    return 0


def cmd_verify_sign(args):
    """The counterparty's side: signature over the checkpoint bytes,
    then the chain, then the verdict — the witness vocabulary drawn
    for the audit log."""
    doc, trusted = ckpt.read_checkpoint(args.ckpt)
    public, anchor = _resolve_public(args)
    if not ckpt.verify_checkpoint_signature(args.ckpt, public):
        raise CliError("the checkpoint signature does not verify "
                       "against this key — the document was tampered "
                       "with or signed by another key")
    path = args.against or audit_log_path()
    records, head, log_sha = _load_and_verify(path)
    verdict = ckpt.check_against_log(doc, records, log_sha)
    report = {
        "schema": CHECKPOINT_VERIFY_SCHEMA,
        "tool": TOOL, "tool_version": VERSION,
        "ckpt": args.ckpt, "anchor": anchor,
        "key_id": doc.get("key_id"),
        "checkpoint_length": doc["length"],
        "checkpoint_head": doc["head"],
        "log": path, "log_length": len(records), "log_head": head,
        "trusted_comment": trusted,
        "verdict": verdict["verdict"], "detail": verdict["detail"],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if verdict["verdict"] in ("held", "byte-identical") else 1
    print("== hls-auditlog verify-sign ==")
    print("  checkpoint: %s (length %d, head %s...)"
          % (args.ckpt, doc["length"], doc["head"][:16]))
    print("  key:        %s (anchor: %s)" % (doc.get("key_id"), anchor))
    print("  log:        %s (%d entr%s now)"
          % (path, len(records), "y" if len(records) == 1 else "ies"))
    print("  verdict:    %s — %s" % (verdict["verdict"], verdict["detail"]))
    return 0 if verdict["verdict"] in ("held", "byte-identical") else 1


def cmd_selftest(_args):
    print("== hls-auditlog selftest ==")
    for name in _selftest_body():
        print("  ok: %s" % name)
    print("  the audit chain is the family's arithmetic, the "
          "checkpoint is the Stage 106 signature")
    return 0


def _selftest_body():
    """The stage's pinned numbers, through the library's front door
    (the CLI wraps this so --json never mixes with the report)."""
    import shutil
    import tempfile

    from hlaudit_parts.hal_common import chain_of
    from hlsign_parts import hlsign_ed25519 as ed

    names = []

    # 1. the ONE DEFINITION: chain_of must equal hpkg_log's writer
    #    arithmetic on a hand-built canonical record — byte for byte.
    _hpkg = os.path.join(TOOL_DIR, "hpkg_parts")
    if _hpkg not in sys.path:
        sys.path.insert(0, _hpkg)
    from hpkg_log import _build_chained_record
    body = {"kind": "op", "op": "pkg.lock", "actor": "gate@vector",
            "outcome": "ok", "subject": "one-definition@0.0.1",
            "detail": {"reason": "hand-built"}}
    prev = "ab" * 32
    # The writer assigns seq/timestamp/prev_hash itself; the pin is
    # that its stored chain_hash recomputes EXACTLY by chain_of over
    # its own output — the same bytes, the same hash, one definition.
    built = _build_chained_record(dict(body), prev)
    want = built["chain_hash"]
    got = chain_of(built["prev_hash"], built)
    assert want == got, "chain_of diverged from hpkg_log's writer"
    names.append("the chain arithmetic is hpkg_log's writer, pinned "
                 "to a hand-built canonical record")

    # 2. the shape: a malformed entry is refused, not skipped.
    from hlaudit_parts.hal_common import entry_shape_error as ese
    good = dict(body)
    good.update({"seq": 1, "timestamp": 0, "prev_hash": "0" * 64,
                 "chain_hash": "0" * 64})
    ese(good, "<vector>", 1)
    for mutate, why in (
            ({"kind": "claim"}, "a wrong kind"),
            ({"op": ""}, "an empty op"),
            ({"actor": ""}, "an empty actor"),
            ({"outcome": "maybe"}, "an outcome outside the vocabulary")):
        bad = dict(good)
        bad.update(mutate)
        try:
            ese(bad, "<vector>", 1)
        except AuditlogError:
            pass
        else:
            raise AssertionError("the shape accepted %s" % why)
    names.append("the entry shape refuses a wrong kind, op, actor, "
                 "outcome")

    # 3. the chain: append + verify round-trip; a mutated body is
    #    caught at its own line, a REPAIRED hash moves the break to
    #    the next line — either way the replay names it. Refusal
    #    entries sit in the same chain as ok ones.
    tmp = tempfile.mkdtemp(prefix="_s112_selftest_")
    log = os.path.join(tmp, "audit.log")
    for i in range(5):
        hal_log.append_entry(log, {
            "kind": "op", "op": "pkg.lock",
            "actor": "gate@vector",
            "outcome": "refused" if i == 2 else "ok",
            "subject": "vec@0.0.%d" % i, "detail": {"i": i},
            "tool": TOOL, "tool_version": VERSION,
        })
    recs = hal_log.load_log(log)
    assert len(recs) == 5, "the round-trip lost entries"
    head, errors = hal_log.verify_log(recs, log)
    assert not errors and head == recs[-1]["chain_hash"]
    lines = open(log, "rb").read().split(b"\n")
    tampered = lines[1].replace(b"vec@0.0.1", b"vec@0.0.X", 1)
    assert tampered != lines[1], "the tamper did not land"
    lines[1] = tampered
    broken = hal_log.parse_log(b"\n".join(lines), "<tampered>")
    _h1, e1 = hal_log.verify_log(broken, "<tampered>")
    assert len(e1) == 1, "a mutated body is one break, at its own line"
    # the smart tamper: recompute the mutated record's hash — now the
    # NEXT record's prev_hash refuses to chain, and the head moves.
    import json as _json
    rec2 = dict(broken[1])
    rec2["chain_hash"] = chain_of(rec2["prev_hash"], rec2)
    lines[1] = _json.dumps(rec2, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    _h2, e2 = hal_log.verify_log(
        hal_log.parse_log(b"\n".join(lines), "<tampered>"), "<tampered>")
    assert e2 and "line 3" in e2[0][0], "a repaired hash must move " \
        "the break to the next line"
    names.append("the round-trip verifies; a mutated entry is caught, "
                 "a repaired hash moves the break, never past it")

    # 4. the inventory: summarize counts ops, outcomes, actors —
    #    sorted, deterministic.
    inv = hal_log.summarize(recs)
    assert inv["ops"] == {"pkg.lock": 5}
    assert inv["outcomes"] == {"ok": 4, "refused": 1}
    assert inv["actors"] == {"gate@vector": 5}
    names.append("the inventory counts every outcome, refused "
                 "included")

    # 5. the checkpoint verdicts — the witness vocabulary, unit
    #    vectors over the log just built.
    ck = {"length": 3, "head": "h" * 64, "log_sha256": "s" * 64}
    v = ckpt.check_against_log(ck, recs[:2])           # shorter log
    assert v["verdict"] == "rollback", v
    v = ckpt.check_against_log(ck, recs[:3])           # wrong head
    assert v["verdict"] == "rewritten", v
    grown = [dict(r) for r in recs]
    for r in grown:
        if r["seq"] == 3:
            r["chain_hash"] = "h" * 64
    v = ckpt.check_against_log(ck, grown)              # grown prefix
    assert v["verdict"] == "held", v
    v = ckpt.check_against_log(
        dict(ck, length=5, head=recs[-1]["chain_hash"]),
        recs, log_sha256="s" * 64)                     # same bytes
    assert v["verdict"] == "byte-identical", v
    names.append("the checkpoint verdicts: rollback, rewritten, "
                 "held, byte-identical")

    # 6. the signature: a checkpoint over a real log signs and
    #    verifies; a flipped byte refuses; a foreign key refuses.
    seed, public = ed.keypair()
    keyid = fmt.key_id_hex(public)
    doc = ckpt.build_checkpoint(recs, "s" * 64, keyid)
    assert doc["length"] == 5 and doc["ops"] == {"pkg.lock": 5}
    out_dir = tempfile.mkdtemp(prefix="_s112_ckpt_")
    doc_path, sig_path = ckpt.write_checkpoint(
        doc, seed, public, out_dir, "timestamp:0 selftest")
    assert ckpt.verify_checkpoint_signature(doc_path, public)
    raw = open(doc_path, "rb").read()
    bad = raw.replace(b'"length": 5', b'"length": 6')
    assert bad != raw
    bad_path = doc_path + ".bad"
    open(bad_path, "wb").write(bad)
    shutil.copyfile(sig_path, bad_path + ".minisig")
    assert not ckpt.verify_checkpoint_signature(
        bad_path, public), "a flipped byte verified"
    _other_seed, other_pub = ed.keypair()
    assert not ckpt.verify_checkpoint_signature(
        doc_path, other_pub), "a foreign key verified"
    names.append("the checkpoint signs, verifies, and refuses a "
                 "tampered document or a foreign key")

    # 7. the signing key is RFC 8032's own numbers.
    names.extend(ed.selftest())
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    return names


# ---------------------------------------------------------------------------
# CLI wiring.
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL, description="Stage 112 audit-log signing (every "
                               "privileged op hashed + chained).")
    sub = ap.add_subparsers(dest="cmd")

    ls = sub.add_parser("list", help="the ops view of the audit log")
    ls.add_argument("log", nargs="?", default=None,
                    help="the audit log (default $HLS_AUDIT_LOG or "
                         "<repo>/.hls-audit.log)")
    ls.add_argument("--op", default=None,
                    help="only entries for this op")
    ls.add_argument("--outcome", default=None,
                    help="only ok or refused entries")
    ls.add_argument("--actor", default=None,
                    help="only entries by this actor")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list, audit_op=None)

    sh = sub.add_parser("show", help="one entry, full detail")
    sh.add_argument("seq", type=int, help="the entry's seq")
    sh.add_argument("log", nargs="?", default=None)
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(fn=cmd_show, audit_op=None)

    vf = sub.add_parser("verify", help="replay the chain, report")
    vf.add_argument("log", nargs="?", default=None)
    vf.add_argument("--json", action="store_true")
    vf.set_defaults(fn=cmd_verify, audit_op=None)

    sg = sub.add_parser("sign", help="checkpoint the log, sign it")
    sg.add_argument("log", nargs="?", default=None)
    sg.add_argument("-s", "--key", default=None,
                    help="secret key path (default hls-sign.key)")
    sg.add_argument("--out", default=None, metavar="DIR",
                    help="where the checkpoint lands (default cwd)")
    sg.add_argument("--password-env", default=None, metavar="VAR")
    sg.add_argument("--trusted-comment", default=None)
    sg.add_argument("--force", action="store_true",
                    help="re-write an existing checkpoint")
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(fn=cmd_sign, audit_op=hal_ops.AUDIT_CHECKPOINT)

    vs = sub.add_parser("verify-sign", help="verify a signed checkpoint "
                                            "against the current log")
    vs.add_argument("ckpt", help="the checkpoint document")
    vs.add_argument("-p", "--pub", default=None)
    vs.add_argument("-P", "--raw-pub", default=None, metavar="HEX")
    vs.add_argument("--against", default=None, metavar="LOG")
    vs.add_argument("--json", action="store_true")
    vs.set_defaults(fn=cmd_verify_sign, audit_op=None)

    st = sub.add_parser("selftest", help="the stage's pinned numbers")
    st.set_defaults(fn=cmd_selftest, audit_op=None)

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_usage(sys.stderr)
        sys.stderr.write("error: a command is required (list, show, "
                         "verify, sign, verify-sign, selftest)\n")
        return 2
    try:
        return args.fn(args)
    except BrokenPipeError:
        # The reader hung up (`| head`); exit quietly, not with a
        # traceback — the report was delivered as far as it could go.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except (CliError, AuditlogError, fmt.SignError) as ex:
        sys.stderr.write("%s: %s\n" % (TOOL, ex))
        # A refused checkpoint is a privileged op that did not
        # happen — the refusal belongs in the log too (the same
        # rule every hooked tool runs; a read-only command carries
        # no audit_op and records nothing).
        op = getattr(args, "audit_op", None)
        if op:
            hal_ops.record_refused(op, str(ex))
        return 1
    except OSError as ex:
        sys.stderr.write("%s: %s\n" % (TOOL, ex))
        return 1


if __name__ == "__main__":
    sys.exit(main())
