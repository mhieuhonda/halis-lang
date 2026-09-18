#!/usr/bin/env python3
"""Stage 76 wasm-pack-acceptance test.

Verifies the ``hls-wasm-pack`` publish pipeline end-to-end:

1. Import surface (facade + 10 part modules).
2. ``--version`` prints the banner; ``--help`` lists all subcommands.
3. Name/version validation (valid, scoped, invalid incl. traversal).
4. Source scan of the demo (functions, struct, enum, js imports).
5. TypeScript mapping (int/float/bool/str/list/map/Option/tainted).
6. ``build`` (bundler): pkg/ files exist, wasm magic, sizes, manifest.
7. All five targets build + validate (bundler/web/nodejs/deno/no-modules).
8. ``pack``: tarball members, reproducible bytes, unpack round-trip,
   traversal defence (``..`` member rejected).
9. ``check``: good pkg passes; broken pkg (missing wasm / bad hash)
   fails with actionable messages.
10. ``publish`` dry-run (no network): command shape, tag/access
    validation, missing-tarball error. Plus ``new`` scaffolding.

Run::

    python3 tests/wasm_pack_acceptance.py

(Called from ``make wasm-pack-acceptance`` in mk/40-backends.mk.)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
_PKG_DIR = os.path.join(TOOLS_DIR, "hlwasm_pack_parts")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

PY = sys.executable
PACK = os.path.join(TOOLS_DIR, "hlwasm_pack.py")
DEMO = os.path.join(REPO_ROOT, "examples", "wasm_pack_demo.hls")


def section(title: str) -> None:
    print()
    print("=== %s ===" % title)


def check(name: str, ok: bool, detail: str = "") -> bool:
    flag = "OK" if ok else "FAIL"
    print("  [%s] %s%s" % (flag, name, (" — " + detail) if detail else ""))
    if not ok:
        check.failed += 1  # type: ignore[attr-defined]
    return ok
check.failed = 0  # type: ignore[attr-defined]


def run_pack(*args: str, cwd: str | None = None):
    return subprocess.run([PY, PACK] + list(args), capture_output=True,
                          text=True, cwd=cwd or REPO_ROOT, timeout=180)


# ---------------------------------------------------------------------------
# 1. Import surface
# ---------------------------------------------------------------------------

def test_imports() -> None:
    section("1. Import surface (facade + 10 part modules)")
    import hlwasm_pack
    check("hlwasm_pack.main present", hasattr(hlwasm_pack, "main"))
    for mod in ("hwp_common", "hwp_manifest", "hwp_scan", "hwp_types",
                "hwp_glue", "hwp_build", "hwp_pack", "hwp_publish",
                "hwp_validate", "hwp_cli"):
        try:
            __import__(mod)
            check("%s importable" % mod, True)
        except ImportError as ex:
            check("%s importable" % mod, False, str(ex)[:80])


# ---------------------------------------------------------------------------
# 2. CLI surface
# ---------------------------------------------------------------------------

def test_cli() -> None:
    section("2. CLI surface (--version, subcommands)")
    r = run_pack("--version")
    check("--version prints the banner",
          r.returncode == 0 and "hls-wasm-pack v0.95.0-alpha" in r.stdout,
          r.stdout.strip()[:60])
    r = run_pack("--help")
    for sub in ("new", "build", "pack", "publish", "check", "test"):
        check("--help lists '%s'" % sub, sub in r.stdout)
    r = run_pack("build", "--help")
    for flag in ("--target", "--name", "--version", "--wasm-opt",
                 "--opt-level", "--scope"):
        check("build --help lists '%s'" % flag, flag in r.stdout)


# ---------------------------------------------------------------------------
# 3. Name/version validation
# ---------------------------------------------------------------------------

def test_validation() -> None:
    section("3. Name/version validation (npm rules)")
    from hwp_manifest import is_valid_name, is_valid_version
    for good in ("my-lib", "wasm-pack-demo", "@myorg/my-lib",
                 "a.b_c~d-e", "x"):
        check("name %r valid" % good, is_valid_name(good))
    for bad in ("", "UPPER", "has space", "../escape", "@scope",
                "@scope/", "a/b/c", "-lead", ".lead", "node_modules",
                "back\\slash"):
        check("name %r rejected" % bad, not is_valid_name(bad))
    for good in ("0.1.0", "0.95.0-alpha", "1.2.3+build", "10.20.30"):
        check("version %r valid" % good, is_valid_version(good))
    for bad in ("", "1.2", "v1.2.3", "1.02.3", "1.2.3-"):
        check("version %r rejected" % bad, not is_valid_version(bad))


# ---------------------------------------------------------------------------
# 4. Source scan
# ---------------------------------------------------------------------------

def test_scan() -> None:
    section("4. Source scan (demo publish surface)")
    from hwp_scan import exported_functions, scan_file
    surface = scan_file(DEMO)
    fns = {f["name"] for f in surface["functions"]}
    for want in ("pack_add", "pack_greet", "pack_axis_name", "pack_sum_to",
                 "main", "jsffi_on_callback"):
        check("scanned fn %s" % want, want in fns)
    check("params of pack_add",
          any(f["name"] == "pack_add" and len(f["params"]) == 2
              and f["returns"] == "int" for f in surface["functions"]))
    check("struct Vec2 scanned",
          any(s["name"] == "Vec2" and len(s["fields"]) == 5
              for s in surface["structs"]))
    check("enum Axis scanned",
          any(e["name"] == "Axis" and len(e["variants"]) == 3
              for e in surface["enums"]))
    # The demo reaches the JS surface via ``import "std.jsffi"`` (the
    # extern block lives in the module, not the demo file — the scan
    # is a documented single-file heuristic). Scan the module itself.
    from hwp_scan import scan_file as _scan
    jsffi = _scan(os.path.join(REPO_ROOT, "std", "jsffi.hls"))
    check("extern js imports scanned", len(jsffi["js_imports"]) >= 2,
          "%d imports" % len(jsffi["js_imports"]))
    exp = {f["name"] for f in exported_functions(surface)}
    check("main excluded from exports", "main" not in exp)
    check("jsffi_on_callback excluded from exports",
          "jsffi_on_callback" not in exp)
    check("pack_add exported", "pack_add" in exp)


# ---------------------------------------------------------------------------
# 5. TypeScript mapping
# ---------------------------------------------------------------------------

def test_types() -> None:
    section("5. TypeScript mapping (halis -> ts)")
    from hwp_types import halis_to_ts, render_dts
    from hwp_scan import scan_file
    cases = {
        "int": "number", "float": "number", "bool": "boolean",
        "str": "string", "void": "void",
        "list[int]": "Array<number>",
        "map[str, int]": "Record<string, number>",
        "tainted[str]": "string",
        "Option[int]": "number | null",
        "Result[int, str]": "number",
        "Vec2": "Vec2",
        "list[map[str, list[bool]]]":
        "Array<Record<string, Array<boolean>>>",
        "bogus with spaces": "unknown",
    }
    for hls, want in cases.items():
        check("ts(%s) == %s" % (hls, want), halis_to_ts(hls) == want,
              halis_to_ts(hls))
    dts = render_dts(scan_file(DEMO), "wasm-pack-demo")
    for needle in ("export function pack_add(a: number, b: number): number;",
                   "export function pack_greet(name: string): string;",
                   "export interface Vec2 {",
                   'export type Axis = "X" | "Y" | "Both";',
                   "HalisInstance", "export default function init"):
        check("d.ts contains %r" % needle[:45], needle in dts)


# ---------------------------------------------------------------------------
# 6. Build (bundler)
# ---------------------------------------------------------------------------

BUILD_PKG: str = ""


def test_build() -> None:
    section("6. Build (bundler target -> publish-ready pkg/)")
    global BUILD_PKG
    tmp = tempfile.mkdtemp(prefix="hwp_acc_")
    BUILD_PKG = os.path.join(tmp, "pkg")
    r = run_pack("build", DEMO, "--out", BUILD_PKG, "--target", "bundler",
                 "--name", "wasm-pack-demo", "--version", "0.95.0-alpha")
    check("build exits 0", r.returncode == 0, r.stderr.strip()[-120:])
    for fname in ("wasm-pack-demo.wasm", "wasm-pack-demo.js",
                  "wasm-pack-demo.d.ts", "package.json", "README.md",
                  ".pack-manifest.json"):
        check("pkg/%s written" % fname,
              os.path.isfile(os.path.join(BUILD_PKG, fname)))
    with open(os.path.join(BUILD_PKG, "wasm-pack-demo.wasm"), "rb") as f:
        head = f.read(8)
    check("wasm magic + version 1",
          head[:4] == b"\x00asm" and head[4:8] == b"\x01\x00\x00\x00")
    wasm_size = os.path.getsize(os.path.join(BUILD_PKG, "wasm-pack-demo.wasm"))
    check("wasm < 100 KB (publish-ready)", wasm_size < 102400,
          "%d bytes" % wasm_size)
    with open(os.path.join(BUILD_PKG, "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)
    check("package.json name", pkg.get("name") == "wasm-pack-demo")
    check("package.json version", pkg.get("version") == "0.95.0-alpha")
    check("package.json sideEffects false", pkg.get("sideEffects") is False)
    check("package.json module/main/types",
          all(k in pkg for k in ("module", "main", "types")))
    with open(os.path.join(BUILD_PKG, ".pack-manifest.json"),
              encoding="utf-8") as f:
        manifest = json.load(f)
    check("manifest packVersion == 1", manifest.get("packVersion") == 1)
    check("manifest target == bundler", manifest.get("target") == "bundler")
    import hashlib
    for fname, meta in manifest.get("files", {}).items():
        with open(os.path.join(BUILD_PKG, fname), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != meta.get("sha256"):
            check("sha256(%s)" % fname, False, "mismatch")
            break
    else:
        check("manifest sha256 of every file", True,
              "%d files" % len(manifest.get("files", {})))
    with open(os.path.join(BUILD_PKG, "wasm-pack-demo.js"),
              encoding="utf-8", errors="replace") as f:
        js = f.read()
    check("bundler js marker", "hls-wasm-pack target: bundler" in js)
    check("bundler js ESM export", "export default init" in js)


# ---------------------------------------------------------------------------
# 7. All five targets
# ---------------------------------------------------------------------------

def test_targets() -> None:
    section("7. All five pack targets (build + validate)")
    from hwp_validate import check as validate
    markers = {
        "bundler": "hls-wasm-pack target: bundler",
        "web": "hls-wasm-pack target: web",
        "nodejs": "hls-wasm-pack target: nodejs",
        "deno": "hls-wasm-pack target: deno",
        "no-modules": "hls-wasm-pack target: no-modules",
    }
    for target, marker in markers.items():
        tmp = tempfile.mkdtemp(prefix="hwp_tgt_")
        out = os.path.join(tmp, "pkg")
        r = run_pack("build", DEMO, "--out", out, "--target", target,
                     "--name", "wdemo", "--version", "0.1.0")
        if not check("build [%s] exits 0" % target, r.returncode == 0,
                     r.stderr.strip()[-100:]):
            continue
        stem_files = os.listdir(out)
        check("pkg [%s] has .wasm+.js+.d.ts" % target,
              "wdemo.wasm" in stem_files and "wdemo.js" in stem_files
              and "wdemo.d.ts" in stem_files)
        with open(os.path.join(out, "wdemo.js"),
                  encoding="utf-8", errors="replace") as f:
            check("js [%s] marker" % target, marker in f.read())
        if target == "deno":
            check("deno.json hint written",
                  os.path.isfile(os.path.join(out, "deno.json")))
        if target == "nodejs":
            with open(os.path.join(out, "wdemo.js"),
                      encoding="utf-8", errors="replace") as f:
                check("nodejs js CJS entry",
                      "module.exports" in f.read())
        if target == "no-modules":
            with open(os.path.join(out, "wdemo.js"),
                      encoding="utf-8", errors="replace") as f:
                check("no-modules js global", "HalisPack" in f.read())
        ok, _msgs = validate(out)
        check("validate [%s] passes" % target, ok)


# ---------------------------------------------------------------------------
# 8. Pack tarball
# ---------------------------------------------------------------------------

def test_pack() -> None:
    section("8. Pack (tarball + round-trip + traversal defence)")
    from hwp_pack import list_members, pack, unpack
    tmp = tempfile.mkdtemp(prefix="hwp_pack_")
    tgz = os.path.join(tmp, "wpack-0.95.0-alpha.tgz")
    out = pack(BUILD_PKG, tgz)
    check("pack returns the tarball path", out == tgz and os.path.isfile(tgz),
          "%d bytes" % os.path.getsize(tgz))
    members = list_members(tgz)
    check("members under package/", all(m.startswith("package/") for m in members),
          "%d members" % len(members))
    check("tarball holds .wasm+.js+.d.ts+package.json",
          any(m.endswith(".wasm") for m in members)
          and any(m.endswith(".js") for m in members)
          and "package/package.json" in members)
    # Reproducible bytes: pack twice -> identical sha256.
    import hashlib
    tgz2 = os.path.join(tmp, "wpack2.tgz")
    pack(BUILD_PKG, tgz2)
    h1 = hashlib.sha256(open(tgz, "rb").read()).hexdigest()
    h2 = hashlib.sha256(open(tgz2, "rb").read()).hexdigest()
    check("reproducible tarball bytes", h1 == h2)
    # Unpack round-trip.
    dest = os.path.join(tmp, "unpacked")
    extracted = unpack(tgz, dest)
    check("unpack extracts every member", len(extracted) == len(members))
    check("unpacked package.json parses",
          json.load(open(os.path.join(dest, "package", "package.json"),
                         encoding="utf-8")).get("name") == "wasm-pack-demo")
    # Traversal defence: a hostile tarball is rejected, never written.
    evil = os.path.join(tmp, "evil.tgz")
    with tarfile.open(evil, mode="w:gz") as tf:
        for arcname in ("../../evil.txt", "/abs.txt", "package/ok.txt"):
            import io as _io
            data = b"x"
            ti = tarfile.TarInfo(name=arcname)
            ti.size = len(data)
            tf.addfile(ti, _io.BytesIO(data))
    try:
        unpack(evil, os.path.join(tmp, "evil_out"))
        check("hostile tarball rejected", False, "no error raised")
    except ValueError:
        check("hostile tarball rejected", True)
    check("no escape file written",
          not os.path.exists(os.path.join(tmp, "evil.txt")))


# ---------------------------------------------------------------------------
# 9. Check (good vs broken)
# ---------------------------------------------------------------------------

def test_check() -> None:
    section("9. Check (good pkg passes, broken pkg fails cleanly)")
    from hwp_validate import check as validate
    ok, msgs = validate(BUILD_PKG)
    check("good pkg validates", ok, msgs[-1] if msgs else "")
    # Broken 1: wasm removed.
    import shutil
    tmp = tempfile.mkdtemp(prefix="hwp_chk_")
    broken = os.path.join(tmp, "broken")
    shutil.copytree(BUILD_PKG, broken)
    for fname in os.listdir(broken):
        if fname.endswith(".wasm"):
            os.unlink(os.path.join(broken, fname))
    ok2, msgs2 = validate(broken)
    check("missing wasm fails", not ok2)
    check("missing wasm message actionable",
          any("wasm" in m.lower() for m in msgs2 if m.startswith("FAIL:")))
    # Broken 2: tampered js (hash mismatch).
    broken2 = os.path.join(tmp, "broken2")
    shutil.copytree(BUILD_PKG, broken2)
    for fname in os.listdir(broken2):
        if fname.endswith(".js"):
            with open(os.path.join(broken2, fname), "a",
                      encoding="utf-8") as f:
                f.write("\n// tampered\n")
    ok3, msgs3 = validate(broken2)
    check("tampered js fails (sha256)", not ok3)
    check("tamper message actionable",
          any("sha256" in m for m in msgs3 if m.startswith("FAIL:")))
    # CLI surface.
    r = run_pack("check", BUILD_PKG)
    check("CLI check exits 0 on good pkg", r.returncode == 0)
    r = run_pack("check", broken)
    check("CLI check exits 1 on broken pkg", r.returncode == 1)


# ---------------------------------------------------------------------------
# 10. Publish dry-run + new
# ---------------------------------------------------------------------------

def test_publish_new() -> None:
    section("10. Publish dry-run (no network) + new scaffolding")
    from hwp_publish import publish_command
    tmp = tempfile.mkdtemp(prefix="hwp_pub_")
    tgz = os.path.join(tmp, "wpack-0.95.0-alpha.tgz")
    from hwp_pack import pack
    pack(BUILD_PKG, tgz)
    r = run_pack("publish", tgz, "--tag", "next")
    check("publish dry-run exits 0", r.returncode == 0,
          (r.stderr.strip() or r.stdout.strip())[-100:])
    check("dry-run prints the npm command",
          "npm publish" in (r.stderr + r.stdout) and "--tag next" in (r.stderr + r.stdout))
    r = run_pack("publish", tgz, "--access", "bogus")
    check("bad access rejected", r.returncode != 0)
    r = run_pack("publish", os.path.join(tmp, "missing.tgz"))
    check("missing tarball rejected", r.returncode != 0)
    cmd = publish_command(tgz, registry="https://r.example.com/npm/",
                          access="restricted", tag="beta")
    check("publish command shape",
          cmd[:2] == ["npm", "publish"] and "--registry" in cmd
          and cmd[-1] == "https://r.example.com/npm",
          " ".join(cmd)[:90])
    # new scaffolding.
    dest = os.path.join(tmp, "my-lib")
    r = run_pack("new", "my-lib", "--scope", "@myorg", "--dir", dest)
    check("new exits 0", r.returncode == 0, r.stderr.strip()[-80:])
    check("new writes main.hls",
          os.path.isfile(os.path.join(dest, "my-lib.hls")))
    check("new writes hls.pack.toml",
          os.path.isfile(os.path.join(dest, "hls.pack.toml")))
    check("new writes README.md",
          os.path.isfile(os.path.join(dest, "README.md")))
    r = run_pack("new", "BAD NAME!")
    check("new rejects a bad name", r.returncode != 0)


def main() -> int:
    test_imports()
    test_cli()
    test_validation()
    test_scan()
    test_types()
    test_build()
    test_targets()
    test_pack()
    test_check()
    test_publish_new()
    print()
    failed = check.failed  # type: ignore[attr-defined]
    total = " (see sections above for the per-assertion breakdown)"
    if failed:
        print("WASM-PACK ACCEPTANCE: %d assertion(s) FAILED%s" % (failed, total))
        return 1
    print("ACCEPTANCE OK: Stage 76 -- hls-wasm-pack (publish-ready wasm + JS glue)%s" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
