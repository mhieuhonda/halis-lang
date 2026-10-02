#!/usr/bin/env python3
"""hls-sbom — the Stage 104 bill of materials (CycloneDX + SPDX).

Usage:
  python3 tools/hls-sbom.py <entry.hls> [--format cdx|spdx|both]
                            [--stdout] [--out DIR] [--json]
  python3 tools/hls-sbom.py --pkg [DIR] [--format cdx|spdx|both]
                            [--stdout] [--out DIR] [--release] [--json]

Every release ships a bill of materials. Not a promise, a LIST: the
components your software stands on, each with its content hash, and
the dependency edges between them — in the two interchange formats
the industry already reads:

  CycloneDX 1.5 (JSON)   bomFormat CycloneDX, components, dependencies
  SPDX 2.3       (JSON)   SPDX-2.3, packages, relationships, CC0-1.0

Two modes over one component set:

  source   walk the transitive `import` graph of an entry file —
           with hls-audit's OWN walker (the SBOM cannot disagree
           with the audit about what the tree is). Every module
           becomes a component (type file), classified
           toolchain / dependency / workspace exactly as the audit
           classifies it, hashed SHA-256 over its bytes, and
           carrying the audit's effect split as properties
           (hls:kind, hls:effects:intrinsic, hls:effects:surface).
           The entry file is the metadata component (application).

  package  walk the transitive PACKAGE tree — hls-audit's package
           walker again. Every package becomes a component
           (application for the root, library for everything
           underneath), hashed the way hls-pkg hashes it (the
           single FILE for a single-file path dep, the
           deterministic directory hash otherwise) so the SBOM's
           hashes are the lockfile's hashes, byte for byte. When a
           lockfile exists its hashes are the ones stamped; a
           mismatch with the tree is DRIFT and fails the run — an
           SBOM of drifted content describes a tree the lock no
           longer names, and a lie is not a deliverable. A package
           the audit cannot check is fail-closed there and the
           SBOM refuses too (never a clean stamp over an
           unauditable node).

Determinism: the documents are content-addressed. The CycloneDX
serialNumber is a UUID derived from the component hashes (not the
wall clock); the SPDX documentNamespace carries the same digest. Two
runs over an unchanged tree with the same SOURCE_DATE_EPOCH produce
byte-identical documents — the bridge to Stage 105 (reproducible
builds): a release's SBOM can be REBUILT, not just re-emitted.
Without SOURCE_DATE_EPOCH the timestamp is the current UTC time (the
reproducible-builds convention decides, the document records it).

Per release: `--release` is the shipping gate. It requires a
lockfile (a release ships pinned content — no lock, no release),
refuses drift and unauditable packages outright, writes
`hls-sbom-<name>-<version>.cdx.json` and
`hls-sbom-<name>-<version>.spdx.json`, and appends ONE record to the
transparency log chaining the SBOM documents' own hashes into the
same tamper-evident ledger `hls-pkg lock` and `hls-pkg publish` use
(kind "sbom") — a release's list of parts is as auditable as the
parts themselves. Without --release the run is analysis-only: no
files written unless --out says so, the log never touched.

Exit contract: 0 when the documents are produced; 1 on drift, an
unauditable package, a missing lockfile under --release, an
unwritable output directory, or a broken source tree; 2 for usage
errors. --json prints the internal report (schema "hls-sbom/v1")
with the component set, the edges, and the documents' own SHA-256s
for CI consumption. Analysis-only end to end (modulo --out /
--release): nothing is compiled, no artifact changes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOL_DIR)

# Load hls-audit by path (a hyphenated module name is not importable
# the normal way). Its body puts the tool dir, hpkg_parts and the
# repo root on sys.path and imports boot — the same machine the
# compiler runs. The SBOM reuses its walkers so the two tools share
# ONE definition of "what the tree is".
_AUDIT_PATH = os.path.join(TOOL_DIR, "hls-audit.py")
_spec = importlib.util.spec_from_file_location("hls_audit_mod", _AUDIT_PATH)
hls_audit = importlib.util.module_from_spec(_spec)
sys.modules["hls_audit_mod"] = hls_audit
_spec.loader.exec_module(hls_audit)

AuditError = hls_audit.AuditError

# The transparency log is hls-pkg's ledger; the SBOM release record
# chains into the SAME chain (hpkg_parts is on sys.path once the
# hls-audit body has run).
from hpkg_log import transparency_log_append            # noqa: E402

TOOL = "hls-sbom"
VERSION = "0.123.0-alpha"          # the toolchain release this SBOM stamps
CDX_SPEC = "1.5"                   # CycloneDX spec version emitted
SPDX_SPEC = "SPDX-2.3"             # SPDX spec version emitted
SCHEMA = "hls-sbom/v1"
CDX_NAME = "hls-sbom.cdx.json"
SPDX_NAME = "hls-sbom.spdx.json"

_PURL_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
_SPDXID_SAFE = re.compile(r"[^A-Za-z0-9.\-]")


# ---------------------------------------------------------------------------
# Deterministic plumbing.
# ---------------------------------------------------------------------------

def _utc_stamp():
    """The document timestamp: SOURCE_DATE_EPOCH when the environment
    names one (the reproducible-builds contract), else now."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is not None:
        try:
            return datetime.fromtimestamp(int(epoch), timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise AuditError("SOURCE_DATE_EPOCH is not a unix timestamp: %r"
                             % epoch)
    return datetime.now(timezone.utc)


def _rfc3339(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _uuid_from_digest(hex32):
    """A syntactically valid UUID v4-shaped string derived from a
    hex digest — deterministic, content-addressed, NOT random."""
    h = list(hex32[:32])
    h[12] = "4"
    h[16] = "89ab"[int(h[16], 16) % 4]
    s = "".join(h)
    return "%s-%s-%s-%s-%s" % (s[:8], s[8:12], s[12:16], s[16:20], s[20:32])


def _set_digest(lines):
    """One digest over the whole component set: every component's ref
    and its content hash, in sorted order. Two documents describing
    the same tree share it; one flipped byte anywhere changes it."""
    h = hashlib.sha256()
    for ln in sorted(lines):
        h.update(ln.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _sha256_file(path):
    return hls_audit.sha256_file(path)


def _key_to_path(key):
    """A module key (repo-relative inside the repo, absolute outside)
    back to a real filesystem path — the same round-trip the audit
    draws when it renders."""
    if os.path.isabs(key):
        return os.path.realpath(key)
    return os.path.realpath(os.path.join(REPO_ROOT, key))


def _purl(name, version):
    if not _PURL_SAFE.match(name):
        return None
    base = "pkg:generic/halis/%s" % name
    if version and _PURL_SAFE.match(str(version)):
        base += "@%s" % version
    return base


def _spdx_id(prefix, key):
    return "SPDXRef-" + prefix + "-" + _SPDXID_SAFE.sub("-", key)


def _dump(doc):
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Source mode.
# ---------------------------------------------------------------------------

def sbom_source(entry_path):
    """Build the component set from an entry file's import tree, using
    hls-audit's own audit (same loader, same checker fixpoint, same
    classification). Raises AuditError for a broken tree."""
    entry_abs = os.path.realpath(os.path.abspath(entry_path))
    if not os.path.isfile(entry_abs):
        raise AuditError("no such file: %s" % entry_path)
    r = hls_audit.audit_source(entry_abs, None)

    digest = _set_digest("%s|%s" % (m["key"],
                                    _sha256_file(_key_to_path(m["key"])))
                         for m in r["modules"])
    root_key = r["root"]

    components = []
    for m in r["modules"]:
        if m["key"] == root_key:
            continue  # the entry is the metadata component
        comp = {
            "bom-ref": "hls:module:" + m["key"],
            "type": "file",
            "name": m["key"],
            "hashes": [{"alg": "SHA-256",
                        "content": _sha256_file(_key_to_path(m["key"]))}],
            "properties": [
                {"name": "hls:kind", "value": m["kind"]},
                {"name": "hls:effects:intrinsic",
                 "value": ", ".join(m["intrinsic"])},
                {"name": "hls:effects:surface",
                 "value": ", ".join(m["surface"])},
            ],
        }
        if m["kind"] == "toolchain":
            comp["version"] = VERSION  # versioned with the compiler
        components.append(comp)

    root_comp = {
        "bom-ref": "hls:module:" + root_key,
        "type": "application",
        "name": root_key,
        "hashes": [{"alg": "SHA-256", "content": _sha256_file(entry_abs)}],
        "properties": [
            {"name": "hls:kind", "value": "workspace"},
            {"name": "hls:effects:intrinsic",
             "value": ", ".join(
                 next(m for m in r["modules"]
                      if m["key"] == root_key)["intrinsic"])},
            {"name": "hls:effects:surface",
             "value": ", ".join(r["program_surface"])},
        ],
    }

    deps = []
    for src, kids in sorted(r["edges"].items()):
        deps.append({"ref": "hls:module:" + src,
                     "dependsOn": ["hls:module:" + k for k in kids]})

    return {
        "digest": digest,
        "root": root_comp,
        "components": components,
        "dependencies": deps,
        "audit": r,
    }


# ---------------------------------------------------------------------------
# Package mode.
# ---------------------------------------------------------------------------

def sbom_package(root_dir):
    """Build the component set from a package tree, using hls-audit's
    package walk (drift + fail-closed included) and hls-pkg's hashing
    contract. Lockfile hashes, when a lock exists, are the stamped
    ones — the tree was just proven to match them."""
    root_dir = os.path.realpath(os.path.abspath(root_dir))
    if not os.path.isdir(root_dir):
        raise AuditError("not a directory: %s" % root_dir)

    r = hls_audit.audit_package(root_dir, None)
    if r["drift"]:
        names = ", ".join(sorted({d["package"] for d in r["drift"]}))
        raise AuditError("drift between the lockfile and the tree (%s) — "
                         "re-lock; an SBOM of drifted content is a lie"
                         % names)
    if r["unauditable"]:
        names = ", ".join(sorted({u["package"] for u in r["unauditable"]}))
        raise AuditError("unauditable package(s) in the tree (%s) — fail "
                         "closed; no SBOM stamps a package nobody could "
                         "check" % names)

    # Second walk with the SAME walker for the node identities the
    # audit report deliberately abstracts away (dirs + content paths).
    root_node = hls_audit._walk_packages(root_dir, REPO_ROOT, [], {})
    nodes = hls_audit._all_nodes(root_node)
    hash_of = {}
    for node in nodes:
        if node.name not in hash_of:
            hash_of[node.name] = _sha256_tree(node.content_path)

    # When a lockfile exists, its hashes are canonical for the
    # TOP-LEVEL deps (the audit just proved they still match).
    locked = {}
    lock_path = os.path.join(root_dir, "hls-pkg.lock")
    if os.path.isfile(lock_path):
        with open(lock_path, "r") as f:
            lock = json.load(f)
        locked = {p.get("name"): p.get("sha256")
                  for p in (lock.get("packages") or []) if isinstance(p, dict)}

    digest = _set_digest("%s|%s" % (p["name"], hash_of[p["name"]])
                         for p in r["packages"])

    comps, comp_by_name, edges = [], {}, {}
    root_rec = r["packages"][0]
    root_comp = {
        "bom-ref": "hls:pkg:" + root_rec["name"],
        "type": "application",
        "name": root_rec["name"],
        "version": root_rec["version"],
        "hashes": [{"alg": "SHA-256",
                    "content": hash_of[root_rec["name"]]}],
        "properties": [
            {"name": "hls:kind", "value": "root"},
            {"name": "hls:effects:surface",
             "value": ", ".join(root_rec["surface"])},
        ],
    }
    if _purl(root_rec["name"], root_rec["version"]):
        root_comp["purl"] = _purl(root_rec["name"], root_rec["version"])
    comp_by_name[root_rec["name"]] = root_comp

    for p in r["packages"][1:]:
        if p["name"] in comp_by_name:
            continue  # a diamond is one component, two edges in
        sha = locked.get(p["name"]) or hash_of[p["name"]]
        comp = {
            "bom-ref": "hls:pkg:" + p["name"],
            "type": "library",
            "name": p["name"],
            "version": p["version"],
            "hashes": [{"alg": "SHA-256", "content": sha}],
            "properties": [
                {"name": "hls:kind", "value": p["source"]},
                {"name": "hls:depth", "value": str(p["depth"])},
                {"name": "hls:effects:surface",
                 "value": ", ".join(p["surface"])},
            ],
        }
        if _purl(p["name"], p["version"]):
            comp["purl"] = _purl(p["name"], p["version"])
        comp_by_name[p["name"]] = comp
        comps.append(comp)

    def direct_of(node):
        out = []
        for d in node.deps:
            if d.name not in out:
                out.append(d.name)
        return out

    edges[root_rec["name"]] = direct_of(root_node)
    for node in nodes[1:]:
        if node.name in edges:
            continue
        edges[node.name] = direct_of(node)

    dependencies = []
    for name in sorted(edges):
        dependencies.append({
            "ref": "hls:pkg:" + name,
            "dependsOn": ["hls:pkg:" + d for d in sorted(edges[name])],
        })

    return {
        "digest": digest,
        "root": root_comp,
        "components": comps,
        "dependencies": dependencies,
        "audit": r,
        "lockfile": r["lockfile"],
        "hashes": hash_of,
        "root_node": root_node,
        "root_surface": r["root"]["surface"],
        "pkg_version": root_rec["version"],
    }


def _sha256_tree(path):
    return hls_audit._sha256_tree(path)


# ---------------------------------------------------------------------------
# The two documents.
# ---------------------------------------------------------------------------

def build_cdx(sb, stamp):
    """CycloneDX 1.5. The serialNumber is the component digest — the
    document is addressed by what it lists, not by when it was made."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": CDX_SPEC,
        "serialNumber": "urn:uuid:" + _uuid_from_digest(sb["digest"]),
        "version": 1,
        "metadata": {
            "timestamp": _rfc3339(stamp),
            "tools": [{"vendor": "halis-lang", "name": TOOL,
                       "version": VERSION}],
            "component": sb["root"],
        },
        "components": sb["components"],
        "dependencies": sb["dependencies"],
    }


def build_spdx(sb, stamp):
    """SPDX 2.3. Packages carry the same hashes; the relationships
    carry the same edges; dataLicense is CC0-1.0 (the spec's own
    convention for documents)."""
    root = sb["root"]
    root_id = _spdx_id("Package", root["name"])
    pkgs = []
    rels = [{
        "spdxElementId": "SPDXRef-DOCUMENT",
        "relationshipType": "DESCRIBES",
        "relatedSpdxElement": root_id,
    }]

    def ref_to_id(ref):
        return _spdx_id("Package", ref.split(":", 2)[-1])

    def pkg_of(comp, purpose):
        p = {
            "name": comp["name"],
            "SPDXID": _spdx_id("Package", comp["name"]),
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": purpose,
            "checksums": [{"algorithm": "SHA256",
                           "checksumValue": comp["hashes"][0]["content"]}],
            "comment": "; ".join(
                "%s = %s" % (pr["name"], pr["value"])
                for pr in comp.get("properties", [])),
        }
        if "version" in comp:
            p["versionInfo"] = str(comp["version"])
        return p

    pkgs.append(pkg_of(root, "APPLICATION"))
    for comp in sb["components"]:
        pkgs.append(pkg_of(comp, "LIBRARY"))
    # The edges come from the SAME dependency list CycloneDX renders —
    # the two documents cannot disagree with each other either.
    for dep in sb["dependencies"]:
        for kid in dep["dependsOn"]:
            rels.append({
                "spdxElementId": ref_to_id(dep["ref"]),
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": ref_to_id(kid),
            })

    return {
        "spdxVersion": SPDX_SPEC,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "SBOM-for-%s" % root["name"],
        "documentNamespace": "https://halis-lang.dev/sbom/%s/%s"
                             % (root["name"], sb["digest"][:12]),
        "creationInfo": {
            "created": _rfc3339(stamp),
            "creators": ["Tool: %s-%s" % (TOOL, VERSION),
                         "Organization: halis-lang"],
        },
        "packages": pkgs,
        "relationships": rels,
        "documentDescribes": [root_id],
    }


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _write(out_dir, name, text):
    path = os.path.join(out_dir, name)
    try:
        with open(path, "w") as f:
            f.write(text)
    except OSError as ex:
        raise AuditError("cannot write %s (%s)" % (path, ex))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-sbom",
        description="Stage 104 bill of materials (CycloneDX 1.5 + SPDX 2.3).")
    ap.add_argument("entry", nargs="?", default=None,
                    help="entry .hls file (source mode)")
    ap.add_argument("--pkg", nargs="?", const=".", default=None,
                    metavar="DIR",
                    help="SBOM the package tree rooted at DIR "
                         "(default: cwd)")
    ap.add_argument("--format", default="both",
                    choices=["cdx", "spdx", "both"],
                    help="which document to emit (default: both)")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="write the documents into DIR (default: the "
                         "entry's directory / the package directory)")
    ap.add_argument("--stdout", action="store_true",
                    help="print a document to stdout instead of writing "
                         "(one --format only)")
    ap.add_argument("--release", action="store_true",
                    help="the shipping gate: lockfile required, drift and "
                         "unauditable refused, versioned documents written, "
                         "the SBOM's own hashes chained into the "
                         "transparency log")
    ap.add_argument("--json", action="store_true",
                    help="print the internal report (schema hls-sbom/v1)")
    args = ap.parse_args(argv)

    if args.pkg is None and args.entry is None:
        ap.print_usage(sys.stderr)
        sys.stderr.write("error: give an entry file (source mode) or "
                         "--pkg [DIR] (package mode)\n")
        return 2
    if args.pkg is not None and args.entry is not None:
        sys.stderr.write("error: <entry> and --pkg are mutually exclusive "
                         "(one mode per run)\n")
        return 2
    if args.release and args.pkg is None:
        sys.stderr.write("error: --release ships a PACKAGE — pass --pkg\n")
        return 2
    if args.stdout and args.format == "both":
        sys.stderr.write("error: --stdout needs one --format "
                         "(cdx or spdx), not both\n")
        return 2
    if args.stdout and args.out is not None:
        sys.stderr.write("error: --stdout and --out are mutually "
                         "exclusive\n")
        return 2
    if args.release and args.stdout:
        sys.stderr.write("error: --release writes the shipping documents; "
                         "--stdout only inspects one\n")
        return 2

    try:
        stamp = _utc_stamp()
        if args.pkg is not None:
            sb = sbom_package(args.pkg)
            if args.release and sb["lockfile"] != "present":
                raise AuditError(
                    "a release ships pinned content — no lockfile next to "
                    "the manifest; run hls-pkg lock first")
            pkg_dir = os.path.realpath(os.path.abspath(args.pkg))
            default_out = pkg_dir
            rel_name = "%s-%s" % (sb["root"]["name"], sb["pkg_version"])
        else:
            sb = sbom_source(args.entry)
            default_out = os.path.dirname(
                os.path.realpath(os.path.abspath(args.entry)))
            rel_name = None

        cdx = build_cdx(sb, stamp)
        spdx = build_spdx(sb, stamp)
        cdx_text = _dump(cdx)
        spdx_text = _dump(spdx)
        cdx_sha = hashlib.sha256(cdx_text.encode("utf-8")).hexdigest()
        spdx_sha = hashlib.sha256(spdx_text.encode("utf-8")).hexdigest()

        written = []
        if args.json and not args.release:
            # Report-only: the machine-readable report NEVER writes
            # artifacts (CI reads trees; it does not stamp them).
            report = {
                "schema": SCHEMA,
                "mode": "package" if args.pkg is not None else "source",
                "release": False,
                "digest": sb["digest"],
                "root": {"name": sb["root"]["name"],
                         "type": sb["root"]["type"],
                         "sha256": sb["root"]["hashes"][0]["content"]},
                "components": [
                    {"name": c["name"], "type": c["type"],
                     "version": c.get("version"),
                     "sha256": c["hashes"][0]["content"],
                     "properties": {p["name"]: p["value"]
                                    for p in c.get("properties", [])}}
                    for c in sb["components"]],
                "dependencies": sb["dependencies"],
                "documents": {"cdx_sha256": cdx_sha,
                              "spdx_sha256": spdx_sha,
                              "serial_number": cdx["serialNumber"],
                              "namespace": spdx["documentNamespace"]},
                "written": [],
            }
            if args.pkg is not None:
                report["lockfile"] = sb["lockfile"]
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if args.release:
            out_dir = args.out or default_out
            if not os.path.isdir(out_dir):
                raise AuditError("output directory does not exist: %s"
                                 % out_dir)
            written.append(_write(out_dir, "hls-sbom-%s.cdx.json" % rel_name,
                                  cdx_text))
            written.append(_write(out_dir, "hls-sbom-%s.spdx.json" % rel_name,
                                  spdx_text))
            rec = transparency_log_append({
                "kind": "sbom",
                "name": sb["root"]["name"],
                "version": sb["pkg_version"],
                "sha256": sb["root"]["hashes"][0]["content"],
                "components": 1 + len(sb["components"]),
                "cdx_sha256": cdx_sha,
                "spdx_sha256": spdx_sha,
            })
            log_line = ("transparency log seq: %d (chain %s...)"
                        % (rec["seq"], rec["chain_hash"][:16]))
        elif args.stdout:
            log_line = None
        else:
            out_dir = args.out or default_out
            if not os.path.isdir(out_dir):
                raise AuditError("output directory does not exist: %s"
                                 % out_dir)
            if args.format in ("cdx", "both"):
                written.append(_write(out_dir, CDX_NAME, cdx_text))
            if args.format in ("spdx", "both"):
                written.append(_write(out_dir, SPDX_NAME, spdx_text))
            log_line = None

        if args.json:
            report = {
                "schema": SCHEMA,
                "mode": "package" if args.pkg is not None else "source",
                "release": bool(args.release),
                "digest": sb["digest"],
                "root": {"name": sb["root"]["name"],
                         "type": sb["root"]["type"],
                         "sha256": sb["root"]["hashes"][0]["content"]},
                "components": [
                    {"name": c["name"], "type": c["type"],
                     "version": c.get("version"),
                     "sha256": c["hashes"][0]["content"],
                     "properties": {p["name"]: p["value"]
                                    for p in c.get("properties", [])}}
                    for c in sb["components"]],
                "dependencies": sb["dependencies"],
                "documents": {"cdx_sha256": cdx_sha,
                              "spdx_sha256": spdx_sha,
                              "serial_number": cdx["serialNumber"],
                              "namespace": spdx["documentNamespace"]},
                "written": written,
            }
            if args.pkg is not None:
                report["lockfile"] = sb["lockfile"]
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if args.stdout:
            # A pure document: the summary goes to stderr, the
            # document is the ONLY thing on stdout — the CI pipe
            # that asked for --stdout gets one clean JSON object.
            sys.stderr.write("hls-sbom: %s — digest %s\n"
                             % (sb["root"]["name"], sb["digest"][:24]))
            doc = cdx_text if args.format == "cdx" else spdx_text
            print(doc, end="" if doc.endswith("\n") else "\n")
            return 0
        which = []
        if args.format in ("cdx", "both"):
            which.append("CycloneDX 1.5  serial %s"
                         % cdx["serialNumber"].replace("urn:uuid:", ""))
        if args.format in ("spdx", "both"):
            which.append("SPDX 2.3        ns    .../%s"
                         % spdx["documentNamespace"].rsplit("/", 1)[-1])
        print("== hls-sbom: %s ==" % ("release" if args.release else
                                      "bill of materials"))
        for w in which:
            print("  %s" % w)
        print("  root: %s (%d component(s) underneath)"
              % (sb["root"]["name"], len(sb["components"])))
        print("  digest: %s" % sb["digest"][:24])
        for w in written:
            print("  wrote: %s" % w)
        if log_line:
            print("  %s" % log_line)
        return 0
    except AuditError as ex:
        sys.stderr.write("hls-sbom: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
