#!/usr/bin/env python3
"""Stage 112 acceptance gate — hls-auditlog, the audit-log signing
(every privileged op hashed + chained).

Run with `make auditlog-acceptance` (or
`python3 tests/auditlog_acceptance.py`).

Nine sections:

  1. the primitives — the ONE DEFINITION against hpkg_log's own
                       writer, the entry shape refusals, the CLI
                       selftest
  2. the log          — the append/verify round-trip, the inventory,
                       the actor override, strict mode, the caller
                       tool identity
  3. the hooks        — a real package through init/add/lock/publish
                       and real keys through keygen/sign: every op
                       lands with its subject, detail and tool name
  4. the refusals     — a release gate that says no, a profile that
                       refuses, a lock without a manifest: refused
                       ops sit in the SAME chain, reasons attached
  5. the launcher     — a real armed run: the entry carries the
                       command, the exit, the effect surface
  6. the checkpoint   — sign, verify-sign (byte-identical, held),
                       the rollback, the rewritten history, the
                       self-report record at length+1
  7. the ledger       — the repo ledgers stay sound; the audit log
                       is a DIFFERENT file from the transparency
                       ledger, and the audit tool refuses to bless
                       a log that is not an audit log
  8. the demos        — the demo and the ok-test run; interpreter
                       and native agree byte for byte (native half
                       only when a C compiler exists — the gate
                       stays hermetic)
  9. the tools        — hlfmt stable, hllint clean on the new entry
                       sources
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "hpkg_parts"))
sys.path.insert(0, os.path.join(ROOT, "tools", "hltlog_parts"))

AUDITLOG = [sys.executable, os.path.join(ROOT, "tools", "hls-auditlog.py")]
SIGN = [sys.executable, os.path.join(ROOT, "tools", "hls-sign.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
SANDBOX = [sys.executable, os.path.join(ROOT, "tools", "hls-sandbox.py")]
DEMO = "examples/auditlog_demo.hls"
OK_TEST = "tests/ok/feat_stage112_auditlog.hls"
PW_VAR = "HLS_SIGN_PASSWORD"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s112_", dir=os.path.join(ROOT, "tests"))
LOG = os.path.join(TMP, "audit.log")          # every flow's audit log
REPO_AUDIT_LOG = os.path.join(ROOT, ".hls-audit.log")
REPO_TLOG = os.path.join(ROOT, ".hls-pkg-transparency.log")
os.environ["_GATE_S112_HAD_TLOG"] = (
    "1" if os.path.exists(REPO_TLOG) else "0")

BASE_ENV = dict(os.environ)
BASE_ENV["HLS_AUDIT_LOG"] = LOG
BASE_ENV["HLS_AUDIT_ACTOR"] = "gate_s112@test"
BASE_ENV["HLS_SIGN_PASSWORD"] = ""
BASE_ENV.pop("HLS_AUDIT_STRICT", None)


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
    kw.setdefault("env", BASE_ENV)
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


def audit_json(*extra):
    p = run(AUDITLOG + list(extra) + ["--json"])
    rep = None
    if p.stdout:
        try:
            rep = json.loads(p.stdout)
        except ValueError:
            rep = None
    return p, rep


def entries():
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG, "r") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


# ---------------------------------------------------------------------------
print("=== 1. the primitives (one definition, the shape, the selftest) ===")
# ---------------------------------------------------------------------------

from hpkg_log import _build_chained_record                  # noqa: E402
from hlaudit_parts.hal_common import (                      # noqa: E402
    AuditlogError, chain_of, entry_shape_error,
)

body = {"kind": "op", "op": "pkg.lock", "actor": "gate@vector",
        "outcome": "ok", "subject": "vec@0.0.1"}
built = _build_chained_record(dict(body), "ab" * 32)
expect(chain_of(built["prev_hash"], built) == built["chain_hash"],
       "hls-auditlog's chain hash IS hpkg_log's writer (one definition)")

good = dict(body)
good.update({"seq": 1, "timestamp": 0, "prev_hash": "0" * 64,
             "chain_hash": "0" * 64})
try:
    entry_shape_error(good, "<vector>", 1)
    shape_ok = True
except AuditlogError:
    shape_ok = False
expect(shape_ok, "the entry shape accepts a well-formed entry")

shape_refusals = 0
for mutate in ({"kind": "claim"}, {"op": ""}, {"op": "has spaces"},
               {"actor": ""}, {"outcome": "maybe"}, {"detail": "no"}):
    bad_e = dict(good)
    bad_e.update(mutate)
    try:
        entry_shape_error(bad_e, "<vector>", 1)
    except AuditlogError:
        shape_refusals += 1
expect(shape_refusals == 6,
       "the entry shape refuses a wrong kind, op, actor, outcome, "
       "detail (6/6)")

p = run(AUDITLOG + ["selftest"])
expect(p.returncode == 0 and "RFC 8032 vector 1" in p.stdout,
       "the CLI selftest passes (the RFC vectors ride along)")

# ---------------------------------------------------------------------------
print("=== 2. the log (round-trip, inventory, actor, strict mode) ===")
# ---------------------------------------------------------------------------

import hlaudit_parts.hal_log as hal_log                    # noqa: E402
import hlaudit_parts.hal_ops as hal_ops                    # noqa: E402
import hlaudit_parts.hal_common as com                     # noqa: E402

# The library writes to its own scratch log here (the env override
# is what every subprocess flow uses below).
lib_log = os.path.join(TMP, "lib.log")
for i in range(5):
    assert hal_ops.record(hal_ops.PKG_LOCK, "refused" if i == 3 else "ok",
                          subject="lib@0.0.%d" % i, detail={"i": i},
                          tool="hls-pkg", log_path=lib_log,
                          actor="gate_s112@test")
recs = hal_log.load_log(lib_log)
expect(len(recs) == 5 and recs[3]["outcome"] == "refused",
       "the library round-trips ok and refused entries alike")
head, errors = hal_log.verify_log(recs, lib_log)
expect(not errors and head == recs[-1]["chain_hash"],
       "the replay is sound from genesis to head")
inv = hal_log.summarize(recs)
expect(inv["outcomes"] == {"ok": 4, "refused": 1}
       and inv["ops"] == {"pkg.lock": 5}
       and inv["actors"] == {"gate_s112@test": 5},
       "the inventory counts ops, outcomes and actors")
again = hal_log.summarize(hal_log.load_log(lib_log))
expect(again == inv, "the inventory is deterministic over the same bytes")

# strict mode: an unwritable audit log refuses; fail-open warns.
saved = os.environ.get("HLS_AUDIT_STRICT")
os.environ["HLS_AUDIT_STRICT"] = "1"
try:
    hal_ops.record(hal_ops.PKG_LOCK, "ok", subject="strict@0.0.0",
                   log_path=os.path.join(TMP, "no", "such", "dir",
                                         "audit.log"))
    strict_raised = False
except AuditlogError:
    strict_raised = True
finally:
    if saved is None:
        os.environ.pop("HLS_AUDIT_STRICT", None)
    else:
        os.environ["HLS_AUDIT_STRICT"] = saved
expect(strict_raised,
       "strict mode turns an unwritable log into a refusal")
gentle = hal_ops.record(hal_ops.PKG_LOCK, "ok", subject="open@0.0.0",
                        log_path=os.path.join(TMP, "no", "such", "dir",
                                              "audit.log"))
expect(gentle is False,
       "fail-open (the default) warns and keeps the toolchain usable")

p, rep = audit_json("list", lib_log)
expect(p.returncode == 0 and rep["shown"] == 5 and rep["length"] == 5,
       "the CLI reads the same log the library wrote")
expect(rep["entries"][0]["actor"] == "gate_s112@test",
       "the actor override reaches every entry")

# the caller-tool identity comes off the stack.
caller = hal_ops._caller_tool()
expect(caller == "hls-auditlog" or caller.startswith("hl"),
       "the tool identity resolves to a tool script (%r)" % caller)

# ---------------------------------------------------------------------------
print("=== 3. the hooks (a real package, real keys) ===")
# ---------------------------------------------------------------------------

app = os.path.join(TMP, "hookapp")
run(PKG + ["init", "hookapp"], cwd=TMP)
expect(os.path.isdir(app), "hls-pkg init creates the fixture")

dep_dir = os.path.join(ROOT, "tests", "_gate_s112_dep")
write(os.path.join(dep_dir, "hls-pkg.toml"),
      '[package]\nname = "audit_dep"\nversion = "0.1.0"\n')
write(os.path.join(dep_dir, "main.hls"),
      'fn hi() -> int uses IO {\n    println("hi")\n    return 0\n}\n')
MANIFEST = ('[package]\nname = "hookapp"\nversion = "2.3.4"\n\n'
            '[dependencies]\n'
            'audit_dep = { path = "tests/_gate_s112_dep/main.hls" }'
            '\n\n[effects]\nallowed = ["IO", "Fs"]\n')
write(os.path.join(app, "hls-pkg.toml"), MANIFEST)
write(os.path.join(app, "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')

p = run(PKG + ["add", "second_dep", "https://example.invalid/x.git",
               "std/x.hls"], cwd=app)
expect(p.returncode == 0, "hls-pkg add edits the manifest")
# The add edit is real but the git dep is unresolvable by design —
# restore the manifest so lock works; the ADD ENTRY stays recorded.
write(os.path.join(app, "hls-pkg.toml"), MANIFEST)
p = run(PKG + ["lock"], cwd=app)
expect(p.returncode == 0, "hls-pkg lock pins the fixture")
p = run(PKG + ["publish"], cwd=app)
expect(p.returncode == 0, "hls-pkg publish chains the claim")

es = entries()
by_op = {}
for e in es:
    by_op.setdefault(e["op"], []).append(e)
expect(len(by_op.get("pkg.init", [])) == 1
       and by_op["pkg.init"][0]["subject"] == "hookapp",
       "pkg.init is recorded with the package name")
expect(len(by_op.get("pkg.add", [])) == 1
       and by_op["pkg.add"][0]["subject"] == "second_dep"
       and "example.invalid" in
       json.dumps(by_op["pkg.add"][0]["detail"]),
       "pkg.add is recorded with the dep and its source")
expect(len(by_op.get("pkg.lock", [])) == 1
       and by_op["pkg.lock"][0]["subject"] == "hookapp@2.3.4"
       and by_op["pkg.lock"][0]["detail"].get("packages") == 1,
       "pkg.lock is recorded with the package and its dep count")
expect(len(by_op.get("pkg.publish", [])) == 1
       and by_op["pkg.publish"][0]["detail"].get("ledger_seq", 0) > 0,
       "pkg.publish is recorded with the ledger seq it produced")
expect(all(e["tool"] == "hls-pkg" for e in es),
       "every pkg entry names its tool")

kkey, kpub = os.path.join(TMP, "g.key"), os.path.join(TMP, "g.pub")
p = run(SIGN + ["keygen", "-f", kkey, "-p", kpub, "-c", "gate"])
expect(p.returncode == 0, "hls-sign keygen mints the fixture key")
kid = None
with open(kpub) as f:
    for ln in f:
        if ln.startswith("untrusted comment: minisign public key "):
            kid = ln.strip().split()[5]
expect(kid is not None, "the fixture key id parses from the pub file")
p = run(SIGN + ["sign", os.path.join(app, "main.hls"), "-s", kkey])
expect(p.returncode == 0, "hls-sign sign signs the entry source")
es = entries()
signs = [e for e in es if e["op"] == "sign.sign"]
keygens = [e for e in es if e["op"] == "sign.keygen"]
expect(len(keygens) == 1 and keygens[0]["subject"] == kid,
       "sign.keygen is recorded with the key id (never the secret)")
expect(len(signs) == 1
       and signs[0]["subject"].endswith("main.hls"),
       "sign.sign is recorded with the payload it signed")

p, rep = audit_json("list", LOG, "--op", "pkg.lock")
expect(p.returncode == 0 and rep["shown"] == 1,
       "list --op filters the inventory")
p, rep = audit_json("show", str(signs[0]["seq"]), LOG)
expect(p.returncode == 0 and rep["entry"]["detail"].get("key_id"),
       "show exposes the full detail of one entry")

# ---------------------------------------------------------------------------
print("=== 4. the refusals (a refused op is still an op) ===")
# ---------------------------------------------------------------------------

before = len(entries())
os.unlink(os.path.join(app, "hls-pkg.lock"))
p = run(SIGN + ["release", app, "--key", kkey])
expect(p.returncode == 1 and "lockfile" in p.stderr,
       "the release gate refuses without a lockfile")
refused = entries()[before:]
expect(len(refused) == 1 and refused[0]["op"] == "sign.release"
       and refused[0]["outcome"] == "refused"
       and "lockfile" in refused[0]["detail"].get("reason", ""),
       "the refusal is chained with the gate's reason")

before = len(entries())
p = run(SANDBOX + ["run", "--effects", "NotAnEffect", "--", "/bin/echo",
                   "x"])
expect(p.returncode == 1 and "unknown effect" in p.stderr,
       "an unknown effect refuses the launcher")
refused = entries()[before:]
expect(len(refused) == 1 and refused[0]["op"] == "sandbox.run"
       and refused[0]["outcome"] == "refused"
       and "NotAnEffect" in refused[0]["detail"].get("reason", ""),
       "the launcher's refusal is chained with its reason")

before = len(entries())
p = run(PKG + ["lock"], cwd=TMP)          # no manifest in TMP
expect(p.returncode == 1, "hls-pkg lock without a manifest exits 1")
refused = entries()[before:]
expect(len(refused) == 1 and refused[0]["op"] == "pkg.lock"
       and refused[0]["outcome"] == "refused",
       "a failed lock is recorded too (probing shows up as probing)")

p, rep = audit_json("list", LOG, "--outcome", "refused")
expect(p.returncode == 0 and rep["shown"] == 3,
       "list --outcome refused shows exactly the refusals")

# ---------------------------------------------------------------------------
print("=== 5. the launcher (the enforcement entry) ===")
# ---------------------------------------------------------------------------

before = len(entries())
p = run(SANDBOX + ["run", "--effects", "IO", "--", "/bin/echo",
                   "sandboxed-hello"])
expect(p.returncode == 0, "the launcher runs a real command armed")
entry = entries()[before]
expect(entry["op"] == "sandbox.run" and entry["outcome"] == "ok"
       and entry["subject"] == "/bin/echo"
       and entry["detail"].get("exit") == 0
       and entry["detail"].get("effects") == ["IO"],
       "the entry carries the command, exit and effect surface")

# ---------------------------------------------------------------------------
print("=== 6. the checkpoint (sign, verify, rollback, rewrite) ===")
# ---------------------------------------------------------------------------

head_before = len(entries())
p, rep = audit_json("sign", LOG, "-s", kkey, "--out", TMP)
expect(p.returncode == 0 and rep["checkpoint"]["length"] == head_before,
       "auditlog sign pins the log's length")
expect(rep["checkpoint"]["head"] == entries()[head_before - 1]["chain_hash"],
       "the pinned head is the chain hash the log carried")
doc_path = rep["written"][0]
expect(rep["record_seq"] == head_before + 1,
       "the checkpoint op is recorded at length+1 (the log describes "
       "its own checkpointing)")
expect(os.path.exists(doc_path)
       and os.path.exists(doc_path + ".minisig"),
       "the checkpoint pair is written (doc + minisig)")

# determinism: the SAME log state produces the SAME checkpoint bytes
# (the library-level statement — the CLI always appends its own
# record after signing, so two CLI signs never see the same state).
import hlaudit_parts.hal_checkpoint as hal_ckpt        # noqa: E402
from hlsign_parts import hlsign_ed25519 as _ed         # noqa: E402
from hlsign_parts import hlsign_format as _fmt         # noqa: E402
recs_now = hal_log.load_log(LOG)[:head_before]
_seed_b, pub_b = _ed.keypair()
d1 = hal_ckpt.build_checkpoint(recs_now, "s" * 64,
                               _fmt.key_id_hex(pub_b))
d2 = hal_ckpt.build_checkpoint(recs_now, "s" * 64, d1["key_id"])
expect(com.canon_bytes(d1) == com.canon_bytes(d2),
       "a checkpoint is deterministic over an unchanged log")

p = run(AUDITLOG + ["verify-sign", doc_path, "-p", kpub,
                    "--against", LOG])
expect(p.returncode == 0 and "held" in p.stdout,
       "verify-sign: the checkpointed history is a prefix (the log "
       "grew by its own checkpoint record)")

# grow: the checkpoint holds over a longer log
before = len(entries())
run(SIGN + ["sign", os.path.join(app, "main.hls"), "-s", kkey,
            "--sig-out", os.path.join(TMP, "another.minisig")])
expect(len(entries()) == before + 1, "the log grew by one op")
p = run(AUDITLOG + ["verify-sign", doc_path, "-p", kpub,
                    "--against", LOG])
expect(p.returncode == 0 and "held" in p.stdout,
       "verify-sign: held over a grown log")

# rollback: entries below the signed length are gone
rolled = os.path.join(TMP, "rolled.log")
with open(LOG) as f:
    lines = f.readlines()
with open(rolled, "w") as f:
    f.writelines(lines[:rep["checkpoint"]["length"] - 1])
p = run(AUDITLOG + ["verify-sign", doc_path, "-p", kpub,
                    "--against", rolled])
expect(p.returncode == 1 and "rollback" in p.stdout,
       "a log shrunk under a signed promise is ROLLBACK evidence")

# rewritten: a mutated entry breaks the chain before any verdict
rw = os.path.join(TMP, "rw.log")
with open(LOG) as f:
    rw_lines = f.readlines()
rw_lines[0] = rw_lines[0].replace("hookapp", "hookevil", 1)
with open(rw, "w") as f:
    f.writelines(rw_lines)
p = run(AUDITLOG + ["verify-sign", doc_path, "-p", kpub, "--against", rw])
expect(p.returncode == 1 and "chain_hash does not match" in p.stderr,
       "a rewritten entry refuses the checkpoint outright")
p = run(AUDITLOG + ["verify", rw])
expect(p.returncode == 1, "verify names the break on the rewritten log")

# a tampered checkpoint document refuses
bad_doc = os.path.join(TMP, "bad.ckpt.json")
with open(doc_path, "rb") as f:
    doc_raw = f.read()
with open(bad_doc, "wb") as f:
    f.write(doc_raw.replace(b'"length": ' +
                            str(rep["checkpoint"]["length"]).encode(),
                            b'"length": 9999'))
shutil.copyfile(doc_path + ".minisig", bad_doc + ".minisig")
p = run(AUDITLOG + ["verify-sign", bad_doc, "-p", kpub, "--against", LOG])
expect(p.returncode == 1 and "does not verify" in p.stderr,
       "a tampered checkpoint document refuses")

# a foreign key refuses
fk, fp = os.path.join(TMP, "f.key"), os.path.join(TMP, "f.pub")
run(SIGN + ["keygen", "-f", fk, "-p", fp, "-c", "foreign"])
p = run(AUDITLOG + ["verify-sign", doc_path, "-p", fp, "--against", LOG])
expect(p.returncode == 1 and "does not verify" in p.stderr,
       "a checkpoint verifies only against its own key")

# ---------------------------------------------------------------------------
print("=== 7. the ledger (two files, both sound) ===")
# ---------------------------------------------------------------------------

p = run(PKG + ["log", "--verify"])
expect(p.returncode == 0,
       "hls-pkg log --verify: the transparency ledger stays sound")
if os.path.exists(REPO_TLOG):
    expect(REPO_AUDIT_LOG != REPO_TLOG,
           "the audit log and the transparency ledger are two files")
# the audit tool refuses to bless a log that is not an audit log
p = run(AUDITLOG + ["verify", REPO_TLOG])
expect(p.returncode == 1,
       "the audit tool refuses a log whose entries are not audit "
       "entries")
# the repo's own audit log (if any) replays soundly
if os.path.exists(REPO_AUDIT_LOG):
    p = run(AUDITLOG + ["verify", REPO_AUDIT_LOG])
    expect(p.returncode == 0,
           "the repo's own audit log replays soundly")
else:
    print("  [note] no repo audit log yet — the repo-log check is "
          "skipped (the gate stays hermetic)")

# ---------------------------------------------------------------------------
print("=== 8. the demos ===")
# ---------------------------------------------------------------------------

p1 = run([sys.executable, "boot/boot.py", DEMO], env=os.environ)
expect(p1.returncode == 0 and "hashed, chained, signed" in p1.stdout,
       "the demo runs (interpreter)")
p2 = run([sys.executable, "boot/boot.py", OK_TEST], env=os.environ)
expect(p2.returncode == 0 and "checks passed" in p2.stdout,
       "the ok-test runs (interpreter)")

hlc = os.path.join(ROOT, "bin", "hlc")
if os.path.exists(hlc):
    n1c = os.path.join(TMP, "demo_native.c")
    n2c = os.path.join(TMP, "ok_native.c")
    b1 = os.path.join(TMP, "demo_native")
    b2 = os.path.join(TMP, "ok_native")
    cc = shutil.which("cc") or shutil.which("gcc")
    ok1 = cc and run([hlc, DEMO, n1c]).returncode == 0 \
        and run([cc, "-O2", "-o", b1, n1c, "-lm", "-pthread"]
                ).returncode == 0
    ok2 = cc and run([hlc, OK_TEST, n2c]).returncode == 0 \
        and run([cc, "-O2", "-o", b2, n2c, "-lm", "-pthread"]
                ).returncode == 0
    if ok1 and ok2:
        r1 = run([b1], env=os.environ)
        r2 = run([b2], env=os.environ)
        expect(r1.returncode == 0 and r1.stdout == p1.stdout,
               "the demo agrees byte for byte with the native binary")
        expect(r2.returncode == 0 and r2.stdout == p2.stdout,
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
    run([sys.executable, "tools/hlfmt.py", "-w", p1f], env=os.environ)
    shutil.copyfile(p1f, p2f)
    run([sys.executable, "tools/hlfmt.py", "-w", p2f], env=os.environ)
    with open(p1f) as fh:
        once = fh.read()
    with open(p2f) as fh:
        twice = fh.read()
    expect(once == twice, "hlfmt: stable formatting on %s" % f)

lints = [run([sys.executable, "tools/hllint.py", f], env=os.environ)
         for f in (DEMO, OK_TEST)]
if all(l.returncode == 0 for l in lints):
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings on the new sources")

# ---------------------------------------------------------------------------
# Gate scratch hygiene. The package flows appended to the repo's
# TRANSPARENCY ledger (its path is fixed by convention) — remove it
# when the gate created it, so a fresh checkout stays fresh.
# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(dep_dir, ignore_errors=True)

had_tlog = os.environ.get("_GATE_S112_HAD_TLOG")
if had_tlog == "0":
    for f in (REPO_TLOG, REPO_TLOG + ".lock"):
        if os.path.exists(f):
            try:
                os.unlink(f)
            except OSError:
                pass

print()
print("Stage 112 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
