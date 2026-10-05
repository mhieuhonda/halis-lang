#!/usr/bin/env python3
"""hls-sign — Stage 106 signed packages (minisign, Ed25519).

Usage:
  python3 tools/hls-sign.py keygen [-f SECRET] [-p PUBLIC] [-c COMMENT]
                                   [--password-env VAR] [--iterations N]
  python3 tools/hls-sign.py sign [-s SECRET] FILE [--sig-out PATH]
                                 [--trusted-comment STR]
                                 [--password-env VAR] [--json]
  python3 tools/hls-sign.py verify [-p PUB | -P HEX] FILE [SIG] [--json]
  python3 tools/hls-sign.py release DIR [--key SECRET] [--out DIR]
                                [--trusted-comment STR]
                                [--password-env VAR] [--json]
  python3 tools/hls-sign.py verify-release DIR [-p PUB | -P HEX] [--json]
  python3 tools/hls-sign.py selftest

Every release record in the transparency log is a CLAIM — publish
claims the content, the SBOM claims the parts, repro claims the bytes
— and a claim without a name behind it is worth exactly the paper it
is printed on. This stage puts a NAME behind the release: an Ed25519
keypair (the minisign discipline), a minisign-format detached
signature over a deterministic release statement, and a verify path
that refuses anything the signer did not sign.

The crypto is RFC-pinned, not folklore: the Ed25519 module proves
itself against RFC 8032 section 7.1 (deterministic signatures — the
exact signature bytes of all four vectors), the secret-key box
against RFC 8439 (ChaCha20-Poly1305, the sunscreen ciphertext byte
for byte). The public key and signature FILES are minisign-compatible
layouts; the secret key at rest is the toolchain's own box
(PBKDF2-HMAC-SHA256 + ChaCha20-Poly1305), documented in full in
hlsign_parts/hlsign_format.py — honest about being libsodium-free.

Paths and passphrases resolve in one order everywhere: an explicit
flag, then the environment (HLS_SIGN_KEY, HLS_SIGN_PUB,
HLS_SIGN_PASSWORD), then the terminal — so a CI run never hangs on a
hidden prompt and an interactive run never needs flags at all.

The package gate follows the release contract the SBOM established:
the audit passes fail-closed (drift first, then unauditable), a
lockfile is required (pinned content, not vibes), the statement is
deterministic over the tree, and ONE record (kind "sign") chains into
the same ledger lock, publish, the SBOM and repro append to.
`verify-release` is the counterparty's answer: signature, then
lockfile bytes, then the content digest re-derived from the CURRENT
tree — exit 0 means the signer signed exactly these bytes and the
tree is still those bytes.

Exit contract: 0 verified/released; 1 on any refusal (bad signature,
tamper, drift, missing lockfile, missing key, unreadable box); 2 for
usage errors. --json prints the machine report (never a file write in
verify modes — verification is read-only end to end).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from hlsign_parts import hlsign_aead as aead                     # noqa: E402
from hlsign_parts import hlsign_ed25519 as ed                    # noqa: E402
from hlsign_parts import hlsign_format as fmt                    # noqa: E402
from hlsign_parts import hlsign_release as rel                   # noqa: E402
from hlaudit_parts import hal_ops                                # noqa: E402

TOOL = "hls-sign"


class CliError(Exception):
    """A refusal the CLI reports on stderr with exit 1."""


def _password(args, what):
    """The passphrase, in a contract's order: the variable --password-env
    names first; then the well-known HLS_SIGN_PASSWORD (how the paths
    already work: HLS_SIGN_KEY, HLS_SIGN_PUB); then a prompt when a
    terminal is attached; then empty (minisign allows it, the KDF
    still runs). A CI run never hangs on a hidden prompt."""
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
    """The trust anchor: -p file, -P raw hex, then the default file.
    Verification never falls back to a key it was not handed."""
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
                       "(verification verifies AGAINST something)" % pub_path)
    public, _, _ = fmt.read_public(pub_path)
    return public, pub_path


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_keygen(args):
    if args.password_env:
        password = _password(args, "the new secret key")
    elif os.environ.get("HLS_SIGN_PASSWORD") is not None:
        password = os.environ["HLS_SIGN_PASSWORD"]
    else:
        if sys.stdin is not None and sys.stdin.isatty():
            import getpass
            password = getpass.getpass("passphrase for the new secret key "
                                       "(empty ok): ")
        else:
            password = ""
    if args.iterations < 1:
        raise CliError("--iterations wants a positive number")
    seed, public = ed.keypair()
    secret_path = args.secret or fmt.DEFAULT_SECRET_NAME
    pub_path = args.pub or fmt.DEFAULT_PUB_NAME
    for path in (secret_path, pub_path):
        if os.path.exists(path) and not args.force:
            raise CliError("%s already exists — keygen does not overwrite "
                           "keys (--force to say it)" % path)
    fmt.write_secret(secret_path, seed, public, password,
                     iterations=args.iterations, comment=args.comment or "")
    fmt.write_public(pub_path, public, comment=args.comment or "")
    # Stage 112: minting a keypair is the most privileged op the
    # toolchain has — the audit entry names the key id, never the
    # passphrase or the seed.
    hal_ops.record(hal_ops.SIGN_KEYGEN, "ok", subject=fmt.key_id_hex(public),
                   detail={"public": pub_path, "comment": args.comment or ""})
    print("== hls-sign keygen ==")
    print("  key id:   %s" % fmt.key_id_hex(public))
    print("  secret:   %s (encrypted, 0600)" % secret_path)
    print("  public:   %s" % pub_path)
    print("  the public key is the trust anchor — publish it; the secret "
          "key is the name behind every signature — guard it")
    return 0


def cmd_sign(args):
    seed, public, secret_path = _load_secret(args)
    if not os.path.isfile(args.file):
        raise CliError("no such file: %s" % args.file)
    with open(args.file, "rb") as f:
        payload = f.read()
    sig_path = args.sig_out or (args.file + ".minisig")
    trusted = args.trusted_comment or fmt.default_trusted_comment(args.file)
    raw_sig, global_sig = fmt.sign_file(seed, public, payload, trusted)
    fmt.write_signature(sig_path, public, raw_sig, global_sig, trusted)
    hal_ops.record(hal_ops.SIGN_SIGN, "ok", subject=args.file,
                   detail={"signature": sig_path,
                           "key_id": fmt.key_id_hex(public),
                           "bytes": len(payload)})
    if args.json:
        print(json.dumps({
            "schema": "hls-sign/v1",
            "tool": TOOL, "tool_version": rel.VERSION,
            "file": args.file, "signature": sig_path,
            "key_id": fmt.key_id_hex(public),
            "trusted_comment": trusted,
            "sig_sha256": rel._sha256_file(sig_path),
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-sign ==")
    print("  signed:  %s (%d byte(s))" % (args.file, len(payload)))
    print("  key:     %s" % fmt.key_id_hex(public))
    print("  wrote:   %s" % sig_path)
    return 0


def cmd_verify(args):
    public, anchor = _resolve_public(args)
    sig_path = args.sig or (args.file + ".minisig")
    if not os.path.isfile(args.file):
        raise CliError("no such file: %s" % args.file)
    if not os.path.isfile(sig_path):
        raise CliError("no signature at %s" % sig_path)
    with open(args.file, "rb") as f:
        payload = f.read()
    try:
        raw_sig, global_sig, _keyid, trusted = fmt.read_signature(sig_path)
        ok = fmt.verify_signatures(public, payload, raw_sig, global_sig,
                                   trusted)
    except fmt.SignError as ex:
        raise CliError(str(ex))
    if args.json:
        print(json.dumps({
            "schema": "hls-sign-verify/v1",
            "tool": TOOL, "tool_version": rel.VERSION,
            "file": args.file, "signature": sig_path,
            "anchor": anchor,
            "verified": bool(ok),
            "trusted_comment": trusted,
        }, indent=2, sort_keys=True))
        return 0 if ok else 1
    if not ok:
        sys.stderr.write("hls-sign: the signature does NOT verify — the "
                         "payload, the trusted comment or the key is not "
                         "what it claims\n")
        return 1
    print("== hls-sign ==")
    print("  verified: %s" % args.file)
    print("  key:      %s (anchor: %s)" % (fmt.key_id_hex(public), anchor))
    print("  trusted:  %s" % trusted)
    return 0


def cmd_release(args):
    secret_path = args.key or os.environ.get("HLS_SIGN_KEY",
                                             fmt.DEFAULT_SECRET_NAME)
    if not os.path.isfile(secret_path):
        raise CliError("no secret key at %s — a release is signed, run "
                       "keygen first" % secret_path)
    password = _password(args, "the secret key")
    statement, sig_path, st_path, rec = rel.release(
        args.dir, secret_path, password, out_dir=args.out,
        trusted_comment=args.trusted_comment)
    hal_ops.record(hal_ops.SIGN_RELEASE, "ok",
                   subject="%s@%s" % (statement["name"],
                                      statement["version"]),
                   detail={"content_sha256": statement["content_sha256"],
                           "key_id": statement["key_id"],
                           "ledger_seq": rec["seq"]})
    if args.json:
        print(json.dumps({
            "schema": "hls-sign-release/v1",
            "tool": TOOL, "tool_version": rel.VERSION,
            "statement": statement,
            "statement_sha256": rel._sha256_file(st_path),
            "sig_sha256": rel._sha256_file(sig_path),
            "written": [st_path, sig_path],
            "ledger_seq": rec["seq"], "ledger_chain": rec["chain_hash"],
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-sign release ==")
    print("  package:  %s@%s" % (statement["name"], statement["version"]))
    print("  content:  %s (%d file(s))" % (statement["content_sha256"][:24]
                                           + "...", statement["files"]))
    print("  key:      %s" % statement["key_id"])
    print("  wrote:    %s" % st_path)
    print("  wrote:    %s" % sig_path)
    print("  transparency log seq: %d (chain %s...)"
          % (rec["seq"], rec["chain_hash"][:16]))
    return 0


def cmd_verify_release(args):
    public, anchor = _resolve_public(args)
    statement = rel.verify_release(args.dir, public=public)
    if args.json:
        print(json.dumps({
            "schema": "hls-sign-verify-release/v1",
            "tool": TOOL, "tool_version": rel.VERSION,
            "dir": os.path.realpath(os.path.abspath(args.dir)),
            "anchor": anchor,
            "verified": True,
            "statement": statement,
        }, indent=2, sort_keys=True))
        return 0
    print("== hls-sign verify-release ==")
    print("  verified: %s@%s signed with key %s (anchor: %s)"
          % (statement["name"], statement["version"], statement["key_id"],
             anchor))
    print("  content:  %s — the tree IS the signed bytes"
          % (statement["content_sha256"][:24] + "..."))
    return 0


def cmd_selftest(_args):
    print("== hls-sign selftest ==")
    e_names = ed.selftest()
    for n in e_names:
        print("  ok: %s" % n)
    a_names = aead.selftest()
    for n in a_names:
        print("  ok: %s" % n)
    print("  the primitives are the RFCs' numbers, not the docs' promises")
    return 0


# ---------------------------------------------------------------------------
# CLI wiring.
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL, description="Stage 106 signed packages (minisign, "
                               "Ed25519).")
    sub = ap.add_subparsers(dest="cmd")

    kg = sub.add_parser("keygen", help="write a new keypair")
    kg.add_argument("-f", "--secret", default=None,
                    help="secret key path (default hls-sign.key)")
    kg.add_argument("-p", "--pub", default=None,
                    help="public key path (default hls-sign.pub)")
    kg.add_argument("-c", "--comment", default="",
                    help="a comment line in both key files")
    kg.add_argument("--password-env", default=None, metavar="VAR",
                    help="read the passphrase from this variable")
    kg.add_argument("--iterations", type=int,
                    default=fmt.DEFAULT_ITERATIONS,
                    help="the KDF cost (default %d)"
                         % fmt.DEFAULT_ITERATIONS)
    kg.add_argument("--force", action="store_true",
                    help="overwrite existing key files")
    kg.set_defaults(fn=cmd_keygen, audit_op=hal_ops.SIGN_KEYGEN)

    sg = sub.add_parser("sign", help="a detached minisign signature")
    sg.add_argument("file", help="the payload file")
    sg.add_argument("-s", "--secret", default=None,
                    help="secret key path (default hls-sign.key)")
    sg.add_argument("--sig-out", default=None, metavar="PATH",
                    help="signature path (default FILE.minisig)")
    sg.add_argument("--trusted-comment", default=None,
                    help="the signed comment line (default: minisign's "
                         "timestamp + file)")
    sg.add_argument("--password-env", default=None, metavar="VAR")
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(fn=cmd_sign, audit_op=hal_ops.SIGN_SIGN)

    vf = sub.add_parser("verify", help="verify a detached signature")
    vf.add_argument("file", help="the payload file")
    vf.add_argument("sig", nargs="?", default=None,
                    help="the signature (default FILE.minisig)")
    vf.add_argument("-p", "--pub", default=None,
                    help="public key FILE (the trust anchor)")
    vf.add_argument("-P", "--raw-pub", default=None, metavar="HEX",
                    help="the raw 32-byte public key, hex")
    vf.add_argument("--json", action="store_true")
    vf.set_defaults(fn=cmd_verify)

    rl = sub.add_parser("release", help="sign a package release")
    rl.add_argument("dir", help="the package directory (with the manifest)")
    rl.add_argument("--key", default=None,
                    help="secret key path (default hls-sign.key)")
    rl.add_argument("--out", default=None, metavar="DIR",
                    help="where the statement + signature land")
    rl.add_argument("--trusted-comment", default=None)
    rl.add_argument("--password-env", default=None, metavar="VAR")
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(fn=cmd_release, audit_op=hal_ops.SIGN_RELEASE)

    vr = sub.add_parser("verify-release", help="verify a signed release "
                                               "against the current tree")
    vr.add_argument("dir", help="the package directory")
    vr.add_argument("-p", "--pub", default=None)
    vr.add_argument("-P", "--raw-pub", default=None, metavar="HEX")
    vr.add_argument("--json", action="store_true")
    vr.set_defaults(fn=cmd_verify_release)

    st = sub.add_parser("selftest", help="the RFC 8032 / RFC 8439 vectors")
    st.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_usage(sys.stderr)
        sys.stderr.write("error: a command is required (keygen, sign, "
                         "verify, release, verify-release, selftest)\n")
        return 2
    try:
        return args.fn(args)
    except (CliError, fmt.SignError, rel.AuditError, aead.AeadError,
            ed.Ed25519Error) as ex:
        sys.stderr.write("%s: %s\n" % (TOOL, ex))
        # Stage 112: a refused privileged op is recorded exactly like
        # a completed one — the gate's reason rides in the detail.
        # Read-only commands carry no audit_op and record nothing.
        op = getattr(args, "audit_op", None)
        if op:
            hal_ops.record_refused(op, str(ex))
        return 1


if __name__ == "__main__":
    sys.exit(main())
