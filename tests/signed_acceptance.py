#!/usr/bin/env python3
"""Stage 106 acceptance gate — hls-sign, the signed packages
(minisign, Ed25519).

Run with `make sign-acceptance` (or
`python3 tests/signed_acceptance.py`).

Nine sections:

  1. the primitives — the RFC 8032 vectors (the exact signature
                      bytes, deterministic), the RFC 8439 vectors,
                      the CLI selftest, the PBKDF2 known answers
  2. the keys        — keygen writes both files, the minisign public
                      blob (Ed + id + pk, 56 chars), the self-derived
                      key id, the 0600 secret, the box round trip,
                      the wrong passphrase refused, overwrite refused
  3. the signature   — the four-line minisign file, 88-char lines,
                      verify ok, tampered payload / comment / key
                      refuse, missing pieces refuse, the raw-hex
                      anchor works, re-signing is byte-identical
  4. the contract    — the primitives layer and the CLI agree; the
                      global signature really covers the trusted
                      comment; an empty payload signs and verifies
  5. the statement   — every field pinned, canonical bytes,
                      deterministic over an unchanged tree, and the
                      content digest is hls-pkg publish's own number
  6. the release     — no lockfile, no release; a fresh lock
                      releases (versioned files, one "sign" record);
                      verify-release fresh / wrong key / tampered
                      statement / tampered content / tampered
                      lockfile; drift refuses BEFORE signing
  7. the ledger      — the chain still verifies; a refused release
                      appended nothing
  8. the demos       — the demo and the ok-test run; interpreter and
                      native agree byte for byte (native half only
                      when bin/hlc exists — the gate stays hermetic)
  9. the tools       — hlfmt stable, hllint clean on the new entry
                      sources
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

SIGN = [sys.executable, os.path.join(ROOT, "tools", "hls-sign.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
DEMO = "examples/signed_demo.hls"
OK_TEST = "tests/ok/feat_stage106_signed.hls"
PW_VAR = "HLS_SIGN_PASSWORD"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s106_", dir=os.path.join(ROOT, "tests"))
LOG_LOCK = os.path.join(ROOT, ".hls-pkg-transparency.log.lock")
LOG_MAIN = os.path.join(ROOT, ".hls-pkg-transparency.log")
DEP_DIR = os.path.join(ROOT, "tests", "_gate_s106_dep")


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


def sign(*extra):
    env = dict(os.environ, **{PW_VAR: "gate-passphrase-106"})
    return run(SIGN + list(extra), env=env)

def sign_json(*extra):
    p = sign(*(list(extra) + ["--json"]))
    return p, (json.loads(p.stdout) if p.returncode == 0 and p.stdout
               else None)


def log_lines():
    if not os.path.exists(LOG_MAIN):
        return 0
    with open(LOG_MAIN, "rb") as f:
        return len(f.readlines())


# The primitives, imported directly — the gate tests the modules and
# the CLI over them, so a CLI bug cannot hide behind a module pass.
from hlsign_parts import hlsign_ed25519 as ed                     # noqa: E402
from hlsign_parts import hlsign_aead as aead                      # noqa: E402
from hlsign_parts import hlsign_format as fmt                     # noqa: E402
from hlsign_parts import hlsign_release as rel                    # noqa: E402

# ---------------------------------------------------------------------------
print("=== 1. the primitives (the RFCs' own numbers) ===")
# ---------------------------------------------------------------------------

e_names = ed.selftest()
expect(len(e_names) == 6 and all("RFC 8032" in n or "keypair" in n
                                 or "refusal" in n for n in e_names),
       "the Ed25519 module passes the RFC 8032 vectors and the refusals")
a_names = aead.selftest()
expect(len(a_names) == 4, "the AEAD module passes the RFC 8439 vectors")

# TEST 1's exact signature bytes — RFC 8032 section 7.1, verbatim.
seed1 = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
pk1 = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
sig1_want = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
expect(ed.sign(seed1, b"") == sig1_want,
       "TEST 1 signs to the RFC's exact bytes (deterministic)")
expect(ed.verify(pk1, b"", sig1_want),
       "TEST 1 verifies against the RFC's public key")
expect(ed.sign(seed1, b"") == ed.sign(seed1, b""),
       "signing is deterministic: same seed, same bytes, same signature")

# The CLI selftest is the same vectors through the tool's front door.
p = run(SIGN + ["selftest"])
expect(p.returncode == 0 and p.stdout.count("  ok: ") == 10
       and "RFC 8032 vector 4" in p.stdout
       and "RFC 8439 AEAD" in p.stdout,
       "the CLI selftest reports all ten vector groups (exit 0)")

# The KDF is hashlib's PBKDF2-HMAC-SHA256 — pinned to the standard
# vectors (password/salt/iterations -> 32 bytes).
expect(aead.kdf_key("password", b"salt", 1) == bytes.fromhex(
           "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b"),
       "PBKDF2 c=1 matches the standard vector")
expect(aead.kdf_key("password", b"salt", 4096) == bytes.fromhex(
           "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"),
       "PBKDF2 c=4096 matches the standard vector")

# ---------------------------------------------------------------------------
print("=== 2. the keys (minisign public, honest secret) ===")
# ---------------------------------------------------------------------------

key_dir = os.path.join(TMP, "keys")
os.makedirs(key_dir)
k_secret = os.path.join(key_dir, "hls-sign.key")
k_pub = os.path.join(key_dir, "hls-sign.pub")
p = sign("keygen", "-f", k_secret, "-p", k_pub, "-c", "gate fixture",
         "--password-env", PW_VAR, "--iterations", "1000")
expect(p.returncode == 0 and os.path.isfile(k_secret)
       and os.path.isfile(k_pub),
       "keygen writes the secret and the public key (exit 0)")
expect("key id:" in p.stdout and "0600" in p.stdout,
       "keygen names the key id and the 0600 mode")

pub_lines = open(k_pub).read().splitlines()
expect(len(pub_lines) == 2
       and pub_lines[0].startswith("untrusted comment: minisign public key"),
       "the public key file is minisign's two-line layout")
blob = fmt.unb64(pub_lines[1], "gate blob")
expect(len(blob) == 42 and blob[:2] == b"Ed" and len(pub_lines[1]) == 56,
       "the blob is Ed(2) + id(8) + pk(32) — 56 base64 characters")
public = blob[10:]
expect(blob[2:10].hex() == fmt.key_id_hex(public),
       "the key id inside the blob is self-derived (sha256(pk)[:8])")
expect(len(fmt.key_id_hex(public)) == 16,
       "the key id is sixteen hex characters")

secret_mode = stat.S_IMODE(os.stat(k_secret).st_mode)
expect(secret_mode == 0o600, "the secret key file is 0600")

pw = "gate-passphrase-106"
seed_rd, public_rd, _kid, _c = fmt.read_secret(k_secret, pw)
expect(ed.public_from_seed(seed_rd) == public and public_rd == public,
       "the box round-trips: the seed derives the very public key "
       "keygen wrote")

wrong = run(SIGN + ["sign", k_pub, "-s", k_secret],
            env=dict(os.environ, **{PW_VAR: "not-the-passphrase"}))
expect(wrong.returncode == 1 and "cannot be opened" in wrong.stderr,
       "the wrong passphrase refuses (exit 1, the AEAD says so)")
secret_bytes = open(k_secret, "rb").read()
expect(public.hex().encode() not in secret_bytes
       and b"gate fixture" not in secret_bytes.splitlines()[1],
       "the secret file carries no plaintext key material")

p2 = sign("keygen", "-f", k_secret, "-p", k_pub, "--iterations", "1000")
expect(p2.returncode == 1 and "does not overwrite" in p2.stderr,
       "keygen refuses to overwrite a key without --force")

# ---------------------------------------------------------------------------
print("=== 3. the signature (the minisign discipline) ===")
# ---------------------------------------------------------------------------

sig_dir = os.path.join(TMP, "sigs")
os.makedirs(sig_dir)
payload = os.path.join(sig_dir, "payload.txt")
write(payload, "the release the name goes behind\n")
p = sign("sign", payload, "-s", k_secret,
         "--trusted-comment", "timestamp:0\tfile:payload.txt")
expect(p.returncode == 0 and os.path.isfile(payload + ".minisig"),
       "sign writes the detached signature (exit 0)")

sig_lines = open(payload + ".minisig").read().splitlines()
expect(len(sig_lines) == 4
       and sig_lines[0].startswith("untrusted comment: signature from "
                                   "hls-sign key "),
       "the signature file is minisign's four-line contract")
expect(len(sig_lines[1]) == 88 and len(sig_lines[3]) == 88,
       "both signature lines are 88 base64 characters (Ed + sig64)")
raw_sig, global_sig, sig_keyid, trusted = fmt.read_signature(
    payload + ".minisig")
expect(sig_keyid == fmt.key_id_hex(public),
       "the untrusted comment names the signing key id")
expect(trusted == "timestamp:0\tfile:payload.txt",
       "the trusted comment rides through signed")

p = sign("verify", payload, "-p", k_pub)
expect(p.returncode == 0 and "verified" in p.stdout,
       "the signature verifies (exit 0)")

with open(payload, "a") as f:
    f.write("one flipped line\n")
p = sign("verify", payload, "-p", k_pub)
expect(p.returncode == 1 and "does NOT verify" in p.stderr,
       "a tampered payload refuses (exit 1)")
with open(payload, "w") as f:
    f.write("the release the name goes behind\n")

tampered_sig = payload + ".minisig"
orig_sig_text = open(tampered_sig).read()
write(tampered_sig, orig_sig_text.replace(
    "trusted comment: timestamp:0\tfile:payload.txt",
    "trusted comment: timestamp:99\tfile:payload.txt"))
p = sign("verify", payload, "-p", k_pub)
expect(p.returncode == 1,
       "a rewritten trusted comment refuses (the global signature holds)")
write(tampered_sig, orig_sig_text)

k2_secret = os.path.join(key_dir, "second.key")
k2_pub = os.path.join(key_dir, "second.pub")
sign("keygen", "-f", k2_secret, "-p", k2_pub, "--password-env", PW_VAR,
     "--iterations", "1000")
p = sign("verify", payload, "-p", k2_pub)
expect(p.returncode == 1 and "does NOT verify" in p.stderr,
       "another key refuses the signature (a trust anchor is not a vibe)")

p = sign("verify", payload, payload + ".minisig",
         "-P", public.hex())
expect(p.returncode == 0,
       "the raw-hex anchor (-P) verifies the same signature")
p = sign("verify", payload, payload + ".minisig", "-P", "ab" * 32)
expect(p.returncode == 1, "a wrong raw-hex anchor refuses")
p = sign("verify", payload, os.path.join(sig_dir, "missing.minisig"),
         "-p", k_pub)
expect(p.returncode == 1 and "no signature" in p.stderr,
       "a missing signature file refuses")
p = sign("verify", os.path.join(sig_dir, "missing.txt"), "-p", k_pub)
expect(p.returncode == 1 and "no such file" in p.stderr,
       "a missing payload refuses")

p = sign("sign", payload, "-s", k_secret,
         "--trusted-comment", "timestamp:0\tfile:payload.txt")
expect(open(payload + ".minisig").read() == orig_sig_text,
       "re-signing the same bytes is byte-identical (determinism)")

# ---------------------------------------------------------------------------
print("=== 4. the contract (the layers agree) ===")
# ---------------------------------------------------------------------------

with open(payload, "rb") as f:
    payload_bytes = f.read()
raw2, glob2, _kid2, trusted2 = fmt.read_signature(payload + ".minisig")
expect(fmt.verify_signatures(public, payload_bytes, raw2, glob2, trusted2),
       "the primitives layer verifies what the CLI verified")
expect(not ed.verify(public, payload_bytes, glob2),
       "the global signature is NOT a second payload signature — it "
       "covers the comment")
empty_payload = os.path.join(sig_dir, "empty.bin")
write(empty_payload, "")
sign("sign", empty_payload, "-s", k_secret,
     "--trusted-comment", "timestamp:0\tfile:empty.bin")
p = sign("verify", empty_payload, "-p", k_pub)
expect(p.returncode == 0, "an empty payload signs and verifies")

# ---------------------------------------------------------------------------
print("=== 5. the statement (the deterministic claim) ===")
# ---------------------------------------------------------------------------

rel_root = os.path.join(TMP, "rel_app")
write(os.path.join(rel_root, "hls-pkg.toml"),
      '[package]\nname = "rel_app"\nversion = "2.3.4"\n\n'
      '[dependencies]\nrel_dep = { path = "tests/_gate_s106_dep/main.hls" }'
      '\n\n[effects]\nallowed = ["IO", "Fs"]\n')
write(os.path.join(rel_root, "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')
write(os.path.join(DEP_DIR, "hls-pkg.toml"),
      '[package]\nname = "rel_dep"\nversion = "0.1.0"\n')
write(os.path.join(DEP_DIR, "main.hls"),
      'fn sz() -> int uses Fs {\n    return fs_size("a")\n}\n')

sign("keygen", "-f", os.path.join(TMP, "rel.key"),
     "-p", os.path.join(TMP, "rel.pub"), "-c", "release key",
     "--password-env", PW_VAR, "--iterations", "1000")
rel_key = os.path.join(TMP, "rel.key")
rel_pub = os.path.join(TMP, "rel.pub")

# No lockfile, no release — the gate refuses before anything is built.
had_log = os.path.exists(LOG_MAIN)
had_lock_file = os.path.exists(LOG_LOCK)
before = log_lines()
p = sign("release", rel_root, "--key", rel_key)
expect(p.returncode == 1 and "hls-pkg lock" in p.stderr
       and "pinned content" in p.stderr,
       "no lockfile, no release (exit 1, says what to do)")
expect(log_lines() == before and
       not os.path.exists(os.path.join(
           rel_root, "hls-sign-rel_app-2.3.4.release.json")),
       "the refused release wrote nothing anywhere")

lp = run(PKG + ["lock"], cwd=rel_root)
expect(lp.returncode == 0, "hls-pkg lock pins the release fixture")

p, report = sign_json("release", rel_root, "--key", rel_key,
                      "--trusted-comment", "timestamp:0")
expect(p.returncode == 0 and report["schema"] == "hls-sign-release/v1",
       "the locked tree releases (exit 0, the report names the schema)")
st_path = os.path.join(rel_root, "hls-sign-rel_app-2.3.4.release.json")
sig_path = os.path.join(rel_root, "hls-sign-rel_app-2.3.4.minisig")
expect(os.path.isfile(st_path) and os.path.isfile(sig_path),
       "the versioned statement + signature land in the package")

statement = json.load(open(st_path))
expect(statement["schema"] == "hls-release-statement/v1"
       and statement["kind"] == "halis-package-release"
       and statement["name"] == "rel_app"
       and statement["version"] == "2.3.4"
       and statement["algorithm"] == "ed25519"
       and statement["tool"] == "hls-sign"
       and statement["tool_version"] == "0.125.0-alpha",
       "the statement pins every contract field")
expect(statement["files"] == 1
       and statement["key_id"] == fmt.key_id_hex(
           fmt.read_public(rel_pub)[0]),
       "the digest walked the tree and the key id names the signer")
expect(statement["lockfile_sha256"] == sha256_of(
           os.path.join(rel_root, "hls-pkg.lock")),
       "the lockfile's own bytes are pinned in the statement")
text = open(st_path).read()
expect(json.dumps(statement, indent=2, sort_keys=True) + "\n" == text,
       "the statement bytes are canonical (sorted keys, trailing newline)")

# The deterministic claim: the same tree, the same trusted comment —
# the same statement bytes. (The signature too: RFC 8032 signing is
# deterministic, the comment is pinned, so the whole file repeats.)
before = log_lines()
p = sign("release", rel_root, "--key", rel_key,
         "--trusted-comment", "timestamp:0")
expect(p.returncode == 0
       and open(st_path).read() == text,
       "re-releasing the unchanged tree re-writes the same statement")
sig_after = open(sig_path).read()
p = sign("release", rel_root, "--key", rel_key,
         "--trusted-comment", "timestamp:0")
expect(open(sig_path).read() == sig_after,
       "re-releasing re-writes the same signature bytes")

# One definition of the digest: hls-pkg publish's own record must
# carry the SAME content hash the statement signed.
before_pub = log_lines()
pp = run(PKG + ["publish"], cwd=rel_root)
expect(pp.returncode == 0, "hls-pkg publish records the same package")
with open(LOG_MAIN, "rb") as f:
    last_two = [json.loads(ln) for ln in f.readlines()[-2:]]
pub_rec = next(r for r in last_two if r.get("kind") == "publish")
sign_rec = next(r for r in last_two if r.get("kind") == "sign")
expect(pub_rec["sha256"] == statement["content_sha256"],
       "the signed content_sha256 IS the publish digest (one definition)")
del before_pub

# ---------------------------------------------------------------------------
print("=== 6. the release (verify-release, the counterparty) ===")
# ---------------------------------------------------------------------------

p, vr = sign_json("verify-release", rel_root, "-p", rel_pub)
expect(p.returncode == 0 and vr["verified"] is True
       and vr["statement"]["name"] == "rel_app",
       "verify-release: the tree IS the signed bytes (exit 0)")

p = sign("verify-release", rel_root, "-p", k2_pub)
expect(p.returncode == 1 and "does not verify" in p.stderr,
       "verify-release with another key refuses")

tam_st = os.path.join(TMP, "tampered.release.json")
orig_st = open(st_path).read()
write(st_path, orig_st.replace('"files": 1', '"files": 2'))
p = sign("verify-release", rel_root, "-p", rel_pub)
expect(p.returncode == 1 and "does not verify" in p.stderr,
       "a tampered statement refuses (the signature covers the bytes)")
write(st_path, orig_st)

with open(os.path.join(rel_root, "main.hls"), "a") as f:
    f.write("\nfn extra() -> int {\n    return 7\n}\n")
p = sign("verify-release", rel_root, "-p", rel_pub)
expect(p.returncode == 1 and "tree is not what the release describes"
       in p.stderr,
       "content changed after signing refuses (drift, named)")
with open(os.path.join(rel_root, "main.hls"), "w") as f:
    f.write('fn main() -> int uses IO {\n    println("x")\n'
            '    return 0\n}\n')

lock_path = os.path.join(rel_root, "hls-pkg.lock")
lock_orig = open(lock_path).read()
with open(lock_path, "a") as f:
    f.write(" ")
p = sign("verify-release", rel_root, "-p", rel_pub)
expect(p.returncode == 1 and "lockfile is not the one" in p.stderr,
       "a changed lockfile refuses (the release names its lock)")
with open(lock_path, "w") as f:
    f.write(lock_orig)
expect(sign("verify-release", rel_root, "-p", rel_pub).returncode == 0,
       "the pinned lockfile bytes restored — the release verifies again")

# ---------------------------------------------------------------------------
print("=== 7. the ledger (the chain covers the name) ===")
# ---------------------------------------------------------------------------

with open(LOG_MAIN, "rb") as f:
    sign_records = [json.loads(ln) for ln in f.readlines()
                    if json.loads(ln).get("kind") == "sign"]
rec = sign_records[-1]
expect(rec["name"] == "rel_app" and rec["version"] == "2.3.4"
       and rec["key_id"] == statement["key_id"]
       and rec["content_sha256"] == statement["content_sha256"]
       and rec["statement_sha256"] == sha256_of(st_path)
       and rec["sig_sha256"] == sha256_of(sig_path),
       "the sign record chains content, statement and signature hashes")
vp = run(PKG + ["log", "--verify"])
expect(vp.returncode == 0, "the transparency chain still verifies")

# Drift refuses BEFORE signing and appends nothing.
lines_before_drift = log_lines()
with open(os.path.join(DEP_DIR, "main.hls"), "a") as f:
    f.write("\nfn helper() -> int uses Rand {\n"
            "    return rand_int(9)\n}\n")
p = sign("release", rel_root, "--key", rel_key)
expect(p.returncode == 1 and "drift" in p.stderr,
       "content changed under the lock — the release refuses (exit 1)")
expect(log_lines() == lines_before_drift,
       "the refused release appended NOTHING to the log")
p = sign("verify-release", rel_root, "-p", rel_pub)
expect(p.returncode == 1 and "drift" in p.stderr,
       "the drifted tree no longer verifies (the release's own audit "
       "runs on the verify side too)")
p = sign("release", rel_root, "--key", os.path.join(TMP, "no.key"))
expect(p.returncode == 1 and "no secret key" in p.stderr,
       "a release without a key refuses (a claim needs a name)")

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
expect(p1.returncode == 0 and "checks passed: 5" in p1.stdout,
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
print("Stage 106 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
