"""release — the sandbox statement and its ledger record (Stage 110).

The SBOM claims the parts, repro claims the bytes, sign claims the
author — the sandbox statement claims the POSTURE: which syscall
allowlist this package version was shipped to run under. It is the
piece a consumer needs to answer "when I run this, what will it be
ABLE to do at the kernel level" without re-deriving anything, and
its place in the chain is exactly where the other claims sit.

The statement (schema hls-sandbox-statement/v1) is arch-INDEPENDENT
by design: it pins the POLICY (effects, fs mode, default action,
baseline, the sorted syscall NAMES and the constrained ones), not
the BPF bytes — the numbers are the assembler's business, and the
assembler is versioned with the toolchain. content_sha256 is the
digest hls-pkg publish computes (the same sorted walk, one
definition — imported, never re-implemented), the lockfile's bytes
are pinned beside it, and the [sandbox] section that shaped the
policy is embedded so the statement is self-describing.

The release gate follows the family contract: manifest, then the
fail-closed audit (drift FIRST, then unauditable, then violations —
the tree must not merely exist, it must PASS its own declared
policy before anyone pins a sandbox to it), then the lockfile,
then the versioned statement, then ONE record (kind "sandbox")
chained into the same transparency ledger lock, publish, the SBOM,
repro and sign append to.

`verify-release` is the counterparty's side: find exactly one
statement, re-derive the profile from the CURRENT tree, hash-compare
the allowlist, re-check the ledger record's statement hash against
the file, and confirm the record is still IN the log. Exit 0 means:
the pinned posture is this tree's posture, and the ledger saw it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
_TOOLS = os.path.dirname(TOOL_DIR)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from hpkg_log import transparency_log_append                 # noqa: E402
from hpkg_log import transparency_log_lookup                 # noqa: E402

from sandbox_parts import sbx_policy as P                     # noqa: E402

STATEMENT_SCHEMA = "hls-sandbox-statement/v1"
STATEMENT_SUFFIX = ".profile.json"


class ReleaseError(Exception):
    """The release cannot be produced or verified. Fatal (exit 1)."""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _content_digest(pkg_dir):
    """The digest hls-pkg publish computes — the SAME sorted walk,
    imported from the sign tool's module (which imported it from the
    same discipline): one definition of the number across the whole
    supply chain."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hlsign_mod_sbx_rel", os.path.join(_TOOLS, "hlsign_parts",
                                           "hlsign_release.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.content_digest(pkg_dir)


def _resolve_entry(pkg_dir, manifest):
    """The package's entry file: manifest [package].entry when
    declared, else main.hls beside the manifest (the hls-pkg build
    convention)."""
    entry = ((manifest.get("package") or {}).get("entry")
             if isinstance(manifest.get("package"), dict) else None)
    if not entry:
        entry = "main.hls"
    p = os.path.join(pkg_dir, str(entry))
    if not os.path.isfile(p):
        raise ReleaseError("no entry file for the profile to be "
                           "derived from: %s" % p)
    return p


def build_statement(pkg_dir, arch_for_counts="x86_64"):
    """Audit the tree (fail-closed), require the lockfile, derive the
    profile (root surface + the [sandbox] section), and return the
    statement dict."""
    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    manifest_path = os.path.join(pkg_dir, "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        raise ReleaseError("no hls-pkg.toml in %s — a release pins a "
                           "package" % pkg_dir)

    from hpkg_manifest import parse_manifest
    manifest = parse_manifest(manifest_path)

    # -- the fail-closed audit, in the family's order ---------------
    r = P.hls_audit.audit_package(pkg_dir, None)
    if r["drift"]:
        names = ", ".join(sorted({d["package"] for d in r["drift"]}))
        raise ReleaseError("drift between the lockfile and the tree "
                           "(%s) — re-lock; a sandbox over drifted "
                           "content is a lie" % names)
    if r["unauditable"]:
        names = ", ".join(sorted({u["package"] for u in r["unauditable"]}))
        raise ReleaseError("unauditable package(s) in the tree (%s) — "
                           "fail closed; nobody pins a sandbox to a "
                           "package they could not check" % names)
    if r["violations"]:
        raise ReleaseError("the tree violates its own [effects] "
                           "policy (%s) — the audit gate failed; a "
                           "sandbox must not paper over it"
                           % ", ".join(v["effect"] + " via "
                                       + v["package"]
                                       for v in r["violations"]))
    if r["lockfile"] != "present":
        raise ReleaseError("a release ships pinned content — no "
                           "lockfile next to the manifest; run "
                           "hls-pkg lock first")

    # -- the profile: root surface + [sandbox] ----------------------
    sbx = P.parse_sandbox_section(manifest)
    entry = _resolve_entry(pkg_dir, manifest)
    deriv = P.derive_source(entry)
    fs_mode = sbx["fs"] or deriv["fs_mode"]
    fs_origin = ("manifest [sandbox] fs" if sbx["fs"] and
                 sbx["fs"] != deriv["fs_mode"]
                 else deriv["fs_origin"])
    default_action = sbx["default"] or "errno"
    profile = P.Profile(deriv["effects"], fs_mode, fs_origin,
                        default_action, "hosted",
                        extra_allow=sbx["allow"], deny=sbx["deny"])

    rules = profile.rules(arch_for_counts)
    constrained = sorted(
        rules[nr].name for nr in rules
        if not rules[nr].unconditional)
    content_sha, files = _content_digest(pkg_dir)
    root = r["packages"][0]
    return {
        "schema": STATEMENT_SCHEMA,
        "kind": "halis-sandbox-statement",
        "name": root["name"],
        "version": root["version"],
        "effects": profile.effects,
        "fs": {"mode": profile.fs_mode, "origin": profile.fs_origin},
        "default_action": profile.default_action,
        "baseline": profile.baseline,
        "allowlist": sorted(rules[nr].name for nr in rules),
        "constrained": constrained,
        "allow": list(profile.extra_allow),
        "deny": list(profile.deny),
        "absent_notes": sorted(set(profile.notes)),
        "entry_surface": deriv["effects"],
        "content_sha256": content_sha,
        "files": files,
        "lockfile_sha256": _sha256_file(
            os.path.join(pkg_dir, "hls-pkg.lock")),
    }


def release(pkg_dir, out_dir=None):
    """Write the versioned statement and chain ONE record into the
    transparency log. Returns (statement, statement_path, record)."""
    statement = build_statement(pkg_dir)
    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    out_dir = os.path.realpath(os.path.abspath(out_dir or pkg_dir))
    if not os.path.isdir(out_dir):
        raise ReleaseError("output directory does not exist: %s" % out_dir)
    base = "hls-sandbox-%s-%s" % (statement["name"], statement["version"])
    st_path = os.path.join(out_dir, base + STATEMENT_SUFFIX)
    payload = (json.dumps(statement, sort_keys=True, indent=2)
               + "\n").encode("utf-8")
    with open(st_path, "wb") as f:
        f.write(payload)

    rec = transparency_log_append({
        "kind": "sandbox",
        "name": statement["name"],
        "version": statement["version"],
        "content_sha256": statement["content_sha256"],
        "fs": statement["fs"]["mode"],
        "default_action": statement["default_action"],
        "allowlist_count": len(statement["allowlist"]),
        "statement_sha256": hashlib.sha256(payload).hexdigest(),
    })
    return statement, st_path, rec


def verify_release(pkg_dir):
    """The counterparty's side. Returns (statement, checks) where
    checks is the list of verdict lines; raises ReleaseError on the
    first refusal."""
    pkg_dir = os.path.realpath(os.path.abspath(pkg_dir))
    statements = sorted(fn for fn in os.listdir(pkg_dir)
                        if fn.endswith(STATEMENT_SUFFIX))
    if not statements:
        raise ReleaseError("no hls-sandbox-<name>-<version>%s in %s — "
                           "nothing to verify"
                           % (STATEMENT_SUFFIX, pkg_dir))
    if len(statements) > 1:
        raise ReleaseError("several sandbox statements in %s (%s) — "
                           "verify one directory per release"
                           % (pkg_dir, ", ".join(statements)))
    st_path = os.path.join(pkg_dir, statements[0])
    with open(st_path, "rb") as f:
        payload = f.read()
    statement = json.loads(payload.decode("utf-8"))
    if statement.get("schema") != STATEMENT_SCHEMA:
        raise ReleaseError("statement schema is %r, expected %s"
                           % (statement.get("schema"), STATEMENT_SCHEMA))

    checks = []

    # 1. the ledger saw exactly these bytes
    rec = transparency_log_lookup(statement["name"],
                                  statement.get("version"))
    if rec is None:
        raise ReleaseError("no ledger record for %s %s — the statement "
                           "was never chained"
                           % (statement["name"], statement.get("version")))
    if rec.get("kind") != "sandbox":
        raise ReleaseError("ledger record for %s is kind %r, not "
                           "'sandbox'"
                           % (statement["name"], rec.get("kind")))
    if rec.get("statement_sha256") != hashlib.sha256(payload).hexdigest():
        raise ReleaseError("the ledger recorded a DIFFERENT statement "
                           "hash — this file is not the one that was "
                           "chained")
    checks.append("ledger: record %s matches these bytes"
                  % rec.get("seq"))

    # 2. the lockfile bytes
    lock_path = os.path.join(pkg_dir, "hls-pkg.lock")
    if not os.path.isfile(lock_path):
        raise ReleaseError("no lockfile beside the statement")
    now = _sha256_file(lock_path)
    if now != statement.get("lockfile_sha256"):
        raise ReleaseError("lockfile bytes drifted since the statement "
                           "(pinned %s, now %s)"
                           % ((statement.get("lockfile_sha256") or
                               "<none>")[:12], now[:12]))
    checks.append("lockfile: bytes unchanged")

    # 3. the content digest, re-derived from the CURRENT tree
    content_sha, _ = _content_digest(pkg_dir)
    if content_sha != statement.get("content_sha256"):
        raise ReleaseError("content digest drifted — the tree is not "
                           "the tree the posture was pinned to "
                           "(pinned %s, now %s)"
                           % ((statement.get("content_sha256") or
                               "<none>")[:12], content_sha[:12]))
    checks.append("content: digest matches the current tree")

    # 4. the posture, re-derived from the CURRENT tree
    now_st = build_statement(pkg_dir)
    for key in ("effects", "fs", "default_action", "baseline",
                "allowlist", "constrained", "allow", "deny"):
        if now_st.get(key) != statement.get(key):
            raise ReleaseError(
                "the profile drifted: %s is %r in the statement, %r in "
                "the current tree — the pinned posture no longer "
                "describes this package"
                % (key, statement.get(key), now_st.get(key)))
    checks.append("profile: the derived posture is unchanged")

    return statement, checks


__all__ = ["build_statement", "release", "verify_release",
           "ReleaseError", "STATEMENT_SCHEMA", "STATEMENT_SUFFIX"]
