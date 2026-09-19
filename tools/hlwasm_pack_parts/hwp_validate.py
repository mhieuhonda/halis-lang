"""hwp_validate — ``pkg/`` checker for Stage 76.

``check`` verifies a built package WITHOUT running it:

  1. required files present (``.wasm`` + ``.js`` + ``.d.ts`` +
     ``package.json`` + ``README.md`` + ``.pack-manifest.json``);
  2. ``.wasm`` starts with the ``\\0asm`` magic + version 1;
  3. ``.js`` carries the ``hls-wasm-pack target:`` marker matching the
     manifest target, and is non-trivially sized;
  4. ``.d.ts`` is non-empty and mentions the loader API;
  5. ``package.json`` parses, has name/version/main/types/files, and
     its name/version match the manifest;
  6. every manifest-listed file exists and its sha256 matches.

Returns a ``(ok, [messages])`` pair; ``ok`` is True only when every
gate passes. ``test_pkg`` additionally smoke-runs the wasm in Node.js
when ``node`` is installed (SKIP otherwise — same convention as the
Stage 23/24 suite gates).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Tuple

from hwp_common import PACK_FORMAT_VERSION, PACK_TARGETS


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check(pkg_dir: str) -> Tuple[bool, List[str]]:
    """Validate ``pkg_dir``; return ``(ok, messages)``."""
    msgs: List[str] = []

    def fail(m: str) -> None:
        msgs.append("FAIL: " + m)

    def ok(m: str) -> None:
        msgs.append("OK: " + m)

    manifest_path = os.path.join(pkg_dir, ".pack-manifest.json")
    if not os.path.isfile(manifest_path):
        fail(".pack-manifest.json missing (not a built package)")
        return False, msgs
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest: Dict[str, Any] = json.load(f)
    except (OSError, ValueError) as ex:
        fail(".pack-manifest.json unreadable: %s" % ex)
        return False, msgs

    if manifest.get("packVersion") != PACK_FORMAT_VERSION:
        fail("packVersion is %r (expected %d)"
             % (manifest.get("packVersion"), PACK_FORMAT_VERSION))
    else:
        ok("packVersion == %d" % PACK_FORMAT_VERSION)
    target = manifest.get("target")
    if target not in PACK_TARGETS:
        fail("target %r is not one of %s" % (target, ", ".join(PACK_TARGETS)))
    else:
        ok("target == %s" % target)
    files = manifest.get("files", {})
    for req in ("package.json", "README.md"):
        if req not in files:
            fail("manifest does not list required %s" % req)
    # Locate the three artifacts by extension.
    wasm_names = [k for k in files if k.endswith(".wasm")]
    js_names = [k for k in files if k.endswith(".js")]
    dts_names = [k for k in files if k.endswith(".d.ts")]
    if len(wasm_names) != 1:
        fail("expected exactly 1 .wasm in manifest, found %d" % len(wasm_names))
    if len(js_names) != 1:
        fail("expected exactly 1 .js in manifest, found %d" % len(js_names))
    if len(dts_names) != 1:
        fail("expected exactly 1 .d.ts in manifest, found %d" % len(dts_names))

    if wasm_names:
        p = os.path.join(pkg_dir, wasm_names[0])
        if not os.path.isfile(p):
            fail("%s missing" % wasm_names[0])
        else:
            with open(p, "rb") as f:
                head = f.read(8)
            if head[:4] == b"\x00asm" and head[4:8] == b"\x01\x00\x00\x00":
                ok("%s has wasm magic + version 1" % wasm_names[0])
            else:
                fail("%s is not a valid wasm module (bad magic)" % wasm_names[0])
    if js_names:
        p = os.path.join(pkg_dir, js_names[0])
        if not os.path.isfile(p):
            fail("%s missing" % js_names[0])
        else:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
            marker = "hls-wasm-pack target: %s" % target
            if marker in src:
                ok("%s carries the %s marker" % (js_names[0], target))
            else:
                fail("%s missing the %r marker" % (js_names[0], marker))
            if len(src.encode("utf-8")) >= 512:
                ok("%s is non-trivial (%d bytes)"
                   % (js_names[0], len(src.encode("utf-8"))))
            else:
                fail("%s is suspiciously small" % js_names[0])
    if dts_names:
        p = os.path.join(pkg_dir, dts_names[0])
        if not os.path.isfile(p):
            fail("%s missing" % dts_names[0])
        else:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
            if "HalisInstance" in src and "declare" in src:
                ok("%s declares the loader API" % dts_names[0])
            else:
                fail("%s missing the loader API declarations" % dts_names[0])

    pkg_json_path = os.path.join(pkg_dir, "package.json")
    if os.path.isfile(pkg_json_path):
        try:
            with open(pkg_json_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
        except ValueError as ex:
            fail("package.json is not valid JSON: %s" % ex)
            pkg = None
        if isinstance(pkg, dict):
            for key in ("name", "version", "main", "types", "files"):
                if key not in pkg:
                    fail("package.json missing key %r" % key)
            if pkg.get("name") == manifest.get("name"):
                ok("package.json name matches manifest")
            else:
                fail("package.json name %r != manifest %r"
                     % (pkg.get("name"), manifest.get("name")))
            if pkg.get("version") == manifest.get("version"):
                ok("package.json version matches manifest")
            else:
                fail("package.json version %r != manifest %r"
                     % (pkg.get("version"), manifest.get("version")))
            if pkg.get("sideEffects") is False:
                ok("package.json sideEffects == false")
            else:
                fail("package.json must set sideEffects=false")
    else:
        fail("package.json missing")

    for fname, meta in files.items():
        p = os.path.join(pkg_dir, fname)
        if not os.path.isfile(p):
            fail("listed file missing: %s" % fname)
            continue
        if isinstance(meta, dict) and "sha256" in meta:
            actual = _sha256_file(p)
            if actual == meta["sha256"]:
                ok("sha256(%s) matches" % fname)
            else:
                fail("sha256(%s) MISMATCH (pkg/ changed after build)"
                     % fname)

    ok_count = sum(1 for m in msgs if m.startswith("OK: "))
    fail_count = sum(1 for m in msgs if m.startswith("FAIL: "))
    msgs.append("summary: %d passed, %d failed" % (ok_count, fail_count))
    return fail_count == 0, msgs


def test_pkg(pkg_dir: str) -> Tuple[str, str]:
    """Smoke-run a built package in Node.js.

    Returns ``("ok"|"skip"|"fail", detail)``. SKIP when node is absent
    (the Stage 23/24 convention); the wasm is loaded through the
    packed ``.js`` flavour when possible, falling back to the raw
    module bytes when the flavour needs a bundler.
    """
    node = shutil.which("node")
    if node is None:
        return "skip", "node.js not installed"
    ok, msgs = check(pkg_dir)
    if not ok:
        return "fail", "package does not validate: " + "; ".join(
            m for m in msgs if m.startswith("FAIL: "))[:300]
    # Find the wasm + js.
    wasm = js = None
    for fname in os.listdir(pkg_dir):
        if fname.endswith(".wasm") and wasm is None:
            wasm = os.path.join(pkg_dir, fname)
        if fname.endswith(".js") and js is None:
            js = os.path.join(pkg_dir, fname)
    if wasm is None or js is None:
        return "fail", "wasm or js missing"
    runner = (
        "const fs=require('fs');\n"
        "const src=fs.readFileSync(%s,'utf-8');\n"
        "eval(src);\n"
        "const bytes=new Uint8Array(fs.readFileSync(%s));\n"
        "Halis.run(bytes).then(c=>{process.exit(0);})"
        ".catch(e=>{console.error('ERR:'+e.message);process.exit(1);});\n"
        % (json.dumps(js), json.dumps(wasm)))
    try:
        proc = subprocess.run([node, "-e", runner], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:
        return "fail", str(ex)[:200]
    if proc.returncode == 0:
        return "ok", "node smoke-run exited 0"
    return "fail", ("rc=%d %s" % (proc.returncode,
                                  (proc.stderr or "")[-200:]))


__all__ = [
    "check",
    "test_pkg",
]
