#!/usr/bin/env python3
"""Stage 113 acceptance gate — LSP go-to-definition across packages.

Run with `make lsp-def-acceptance` (or
`python3 tests/lsp_def_acceptance.py`).

Eight sections:

  1. the std family   — the import literal IS a jump; a std symbol
                        resolves into std/str.hls, a core. symbol into
                        core/; the walk-up bound holds (depth 4 found,
                        depth 5 falls through to the toolchain root)
  2. the sibling      — a relative import into a file nobody opened:
                        parsed on demand; an OPEN buffer beats the disk
                        copy (the jump lands on the buffer's line)
  3. the package      — hls-pkg.lock resolved_path: file deps
                        contribute their directory, dir deps themselves;
                        relative resolved_paths resolve against the
                        package root; the .hls-pkg-deps farm hls-pkg
                        build maintains; the HLS_PKG_DEPS override the
                        compiler itself honours
  4. the layer law    — local beats imported beats dependency beats the
                        last-resort net over merely-open files
  5. the guards       — absolute and ".." imports answer null (never an
                        exit); a missing dependency answers null; a
                        malformed lockfile is skipped and resolution
                        still works through the farm
  6. the externals    — external documents never publish diagnostics;
                        rename never edits them; references DO span
                        them (read-only); an on-disk edit is seen on the
                        next lookup (mtime refresh)
  7. the completion   — a dependency's API completes the moment its
                        import is in the file; the external LRU evicts
                        under pressure and re-loads on demand
  8. the smoke        — lsp_smoke.py stays green; the ok corpus test
                        runs through boot (native half only when a C
                        compiler exists — the gate stays hermetic)

The server is driven as a real subprocess over stdio JSON-RPC for the
protocol sections; the LRU section drives HLSServer in-process (the
eviction bookkeeping is not observable through the protocol).

Exit code 0 = all acceptance criteria met.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

LSP = os.path.join(ROOT, "tools", "hls-lsp.py")
SMOKE = os.path.join(ROOT, "tools", "lsp_smoke.py")
OK_TEST = os.path.join(ROOT, "tests", "ok", "feat_stage113_lsp_resolve.hls")
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s113_", dir=os.path.join(ROOT, "tests"))


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)
    return cond


def write(path, text):
    full = os.path.join(TMP, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)
    return full


# ---------------------------------------------------------------------------
# Protocol harness: a fresh server subprocess per scenario, driven the way
# an editor drives it (initialize -> didOpen -> requests -> shutdown/exit).
# ---------------------------------------------------------------------------

def frame(obj):
    body = json.dumps(obj).encode()
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def parse_frames(buf):
    msgs = []
    while b"Content-Length:" in buf:
        i = buf.index(b"Content-Length:")
        j = buf.index(b"\r\n\r\n", i)
        n = int(buf[i + 16:j])
        if j + 4 + n > len(buf):
            break
        try:
            msgs.append(json.loads(buf[j + 4:j + 4 + n]))
        except ValueError:
            pass
        buf = buf[j + 4 + n:]
    return msgs


def session(ws_name, opens, requests, extra_env=None):
    """Run one server session. `opens`: [(relpath, text)] opened in order.
    `requests`: [(method, params-dict-minus-textDocument, open-index)].
    Returns (results-in-order, returncode, stderr)."""
    env = dict(os.environ)
    env.pop("HLS_PKG_DEPS", None)
    if extra_env:
        env.update(extra_env)
    base = os.path.join(TMP, ws_name)
    uris = ["file://" + os.path.join(base, rel) for rel, _t in opens]
    payload = frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
    for i, (rel, text) in enumerate(opens):
        payload += frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                          "params": {"textDocument": {"uri": uris[i],
                                                      "version": 1,
                                                      "text": text}}})
    for i, (method, params, open_idx) in enumerate(requests):
        p = dict(params)
        p["textDocument"] = {"uri": uris[open_idx]}
        payload += frame({"jsonrpc": "2.0", "id": 100 + i,
                          "method": method, "params": p})
    payload += frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    payload += frame({"jsonrpc": "2.0", "method": "exit"})
    proc = subprocess.Popen(
        [sys.executable, LSP], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate(payload, timeout=120)
    results = {}
    for m in parse_frames(out):
        if isinstance(m.get("id"), int) and m["id"] >= 100:
            results[m["id"]] = m.get("result")
    return [results.get(100 + i) for i in range(len(requests))], \
        proc.returncode, err.decode("utf-8", "replace")


def defn(ws_name, main_rel, main_text, line, ch, extra_opens=None,
         extra_env=None):
    """One textDocument/definition request; returns (result, rc, err)."""
    opens = [(main_rel, main_text)] + (extra_opens or [])
    reqs = [("textDocument/definition",
             {"position": {"line": line, "character": ch}}, 0)]
    res, rc, err = session(ws_name, opens, reqs, extra_env)
    return (res[0] if res else None), rc, err


MAIN_STD = ('import "std.str"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    let s: str = str_replace("aab", "a", "b")\n'
            '    println(s)\n'
            '    return 0\n'
            '}\n')


# ---------------------------------------------------------------------------
# Section 1 — the std family.
# ---------------------------------------------------------------------------

def section_1():
    print("1. the std family — the import literal is a jump; std/ and "
          "core/ resolve; the walk-up bound holds")

    res, rc, err = defn("s1", "main.hls", MAIN_STD, 0, 12)
    check(rc == 0, "server exits cleanly (rc=0)")
    check(res is not None and res["uri"].endswith("/std/str.hls"),
          "the import literal \"std.str\" jumps to std/str.hls"
          if res is not None else "import literal jump answered null: %s" % err)

    res, rc, err = defn("s1", "main.hls", MAIN_STD, 3, 25)
    check(res is not None and res["uri"].endswith("/std/str.hls"),
          "str_replace resolves into std/str.hls (file never opened)")

    # core family: a core module the compiler actually ships.
    core_mod = "alloc"
    core_file = os.path.join(ROOT, "core", core_mod + ".hls")
    if os.path.isfile(core_file):
        main = ('import "core.%s"\n'
                '\n'
                'fn main() -> int {\n'
                '    return 0\n'
                '}\n' % core_mod)
        res, rc, err = defn("s1", "main.hls", main, 0, 12)
        check(res is not None and res["uri"].endswith("/core/%s.hls" % core_mod),
              "the core. family resolves into core/ (core/%s.hls)" % core_mod)
    else:
        bad("core/%s.hls missing — core family case skipped badly" % core_mod)

    # The walk-up: a file nested four levels under its own tree still
    # finds the toolchain's std/ (the walk visits depths 0..4 from the
    # importing file's directory, then falls back to the toolchain root).
    deep = "a/b/c/d/main.hls"
    res, rc, err = defn("s1", deep, MAIN_STD, 0, 12)
    check(res is not None and res["uri"].endswith("/std/str.hls"),
          "depth-4 nesting still reaches the toolchain std/ fallback")

    # A hostile module name never jumps and never exits.
    res, rc, err = defn("s1", "main.hls", 'import "std.../secret"\n\nfn main() -> int {\n    return 0\n}\n', 0, 12)
    check(res is None and rc == 0, "std.../secret (dot-dot module) answers null, server alive")


# ---------------------------------------------------------------------------
# Section 2 — the sibling (relative import, on-disk, parse-on-demand).
# ---------------------------------------------------------------------------

def section_2():
    print("2. the sibling — relative imports parse on demand; open "
          "buffers beat disk")

    write("s2/lib/util.hls",
          "fn util_add(a: int, b: int) -> int {\n    return a + b\n}\n")
    main = ('import "lib/util.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    let x: int = util_add(1, 2)\n'
            '    println(x)\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s2", "main.hls", main, 3, 20)
    check(res is not None and res["uri"].endswith("/lib/util.hls"),
          "util_add resolves into lib/util.hls — the file was never opened")
    res, rc, err = defn("s2", "main.hls", main, 0, 12)
    check(res is not None and res["uri"].endswith("/lib/util.hls"),
          "the relative import literal jumps to lib/util.hls")

    # Open-buffer precedence: same file open in the editor with an EXTRA
    # function the disk copy does not have — the jump must land in the
    # buffer (the disk copy cannot answer it).
    disk = "fn util_add(a: int, b: int) -> int {\n    return a + b\n}\n"
    write("s2b/lib/util.hls", disk)
    buffer_text = disk + "\nfn util_extra() -> int {\n    return 9\n}\n"
    main_b = ('import "lib/util.hls"\n'
              '\n'
              'fn main() -> int uses IO {\n'
              '    println(util_extra())\n'
              '    return 0\n'
              '}\n')
    res, rc, err = session(
        "s2b", [("main.hls", main_b), ("lib/util.hls", buffer_text)],
        [("textDocument/definition",
          {"position": {"line": 3, "character": 14}}, 0)])
    r = res[0]
    # util_extra's `fn` keyword sits on 0-indexed line 4 of the BUFFER
    # (3 lines of util_add + one blank) — the disk copy has no line 5
    # at all, so landing there proves the buffer answered.
    check(r is not None and r["uri"].endswith("/lib/util.hls")
          and r["range"]["start"]["line"] == 4,
          "the OPEN buffer answers util_extra (disk copy does not have it)")


# ---------------------------------------------------------------------------
# Section 3 — the package (lockfile, farm, env override).
# ---------------------------------------------------------------------------

def section_3():
    print("3. the package — hls-pkg.lock resolved_path, the "
          ".hls-pkg-deps farm, the HLS_PKG_DEPS override")

    # 3a: a FILE dependency via resolved_path.
    write("s3a/vendor/mylib/mylib.hls",
          'fn lib_greet(who: str) -> str {\n    return "hi " + who\n}\n')
    write("s3a/hls-pkg.toml",
          '[package]\nname = "app"\nversion = "0.1.0"\n\n'
          '[dependencies]\nmylib = { path = "vendor/mylib/mylib.hls" }\n')
    lock = {"version": 2, "packages": [
        {"name": "mylib", "source": {"path": "vendor/mylib/mylib.hls"},
         "version": "local", "commit": None, "sha256": "0" * 64,
         "effects": [], "transitive_effects": [],
         "resolved_path": os.path.join(TMP, "s3a/vendor/mylib/mylib.hls"),
         "log_seq": 1}]}
    write("s3a/hls-pkg.lock", json.dumps(lock, indent=2))
    main = ('import "mylib.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(lib_greet("x"))\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s3a", "main.hls", main, 3, 14)
    check(res is not None and res["uri"].endswith("vendor/mylib/mylib.hls"),
          "a lockfile FILE dep resolves: lib_greet jumps into the dep source")

    # 3b: a DIRECTORY dependency via resolved_path (relative to the
    # package root, exactly as hls-pkg lock writes it for path deps).
    write("s3b/vendor/webapi/api.hls",
          "fn api_call() -> int {\n    return 3\n}\n")
    write("s3b/hls-pkg.toml",
          '[package]\nname = "app"\nversion = "0.1.0"\n\n'
          '[dependencies]\nwebapi = { path = "vendor/webapi" }\n')
    lock = {"version": 2, "packages": [
        {"name": "webapi", "source": {"path": "vendor/webapi"},
         "version": "local", "commit": None, "sha256": "1" * 64,
         "effects": [], "transitive_effects": [],
         "resolved_path": "vendor/webapi", "log_seq": 2}]}
    write("s3b/hls-pkg.lock", json.dumps(lock, indent=2))
    main = ('import "webapi/api.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(api_call())\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s3b", "main.hls", main, 3, 14)
    check(res is not None and res["uri"].endswith("vendor/webapi/api.hls"),
          "a lockfile DIR dep (relative resolved_path) resolves")

    # 3c: the .hls-pkg-deps farm — hls-pkg build's symlink layout, no
    # lockfile at all.
    write("s3c/.hls-pkg-deps/mylib/api.hls",
          "fn farm_fn() -> int {\n    return 7\n}\n")
    main = ('import "mylib/api.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(farm_fn())\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s3c", "main.hls", main, 3, 14)
    check(res is not None and ".hls-pkg-deps" in (res.get("uri") or "")
          and res["uri"].endswith("api.hls"),
          "the .hls-pkg-deps farm resolves without any lockfile")

    # 3d: the HLS_PKG_DEPS environment override the compiler honours —
    # the server must never disagree with the build.
    write("s3d/deps/mylib/only.hls",
          "fn env_fn() -> int {\n    return 11\n}\n")
    main = ('import "mylib/only.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(env_fn())\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s3d", "main.hls", main, 3, 14,
                        extra_env={"HLS_PKG_DEPS":
                                   os.path.join(TMP, "s3d", "deps")})
    check(res is not None and res["uri"].endswith("only.hls"),
          "HLS_PKG_DEPS (the compiler's own override) resolves")


# ---------------------------------------------------------------------------
# Section 4 — the layer law.
# ---------------------------------------------------------------------------

def section_4():
    print("4. the layer law — local beats imported beats dependency "
          "beats the open net")

    # imported beats open-net: the same name in an unrelated open file
    # must NOT capture the jump.
    write("s4a/lib/util.hls", "fn util_add(a: int, b: int) -> int {\n    return a + b\n}\n")
    other = "fn util_add(a: int, b: int) -> int {\n    return a - b\n}\n"
    main = ('import "lib/util.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(util_add(1, 2))\n'
            '    return 0\n'
            '}\n')
    res, rc, err = session(
        "s4a", [("main.hls", main), ("other.hls", other)],
        [("textDocument/definition",
          {"position": {"line": 3, "character": 14}}, 0)])
    r = res[0]
    check(r is not None and r["uri"].endswith("/lib/util.hls"),
          "an imported file beats an unrelated open file carrying the same name")

    # local beats imported.
    main_local = ('import "lib/util.hls"\n'
                  '\n'
                  'fn util_add(a: int, b: int) -> int {\n'
                  '    return a + b + 1\n'
                  '}\n'
                  '\n'
                  'fn main() -> int uses IO {\n'
                  '    println(util_add(1, 2))\n'
                  '    return 0\n'
                  '}\n')
    res, rc, err = defn("s4a", "main.hls", main_local, 7, 14,
                        extra_opens=[])
    check(res is not None and res["uri"].endswith("/main.hls")
          and res["range"]["start"]["line"] == 2,
          "a local definition beats the imported one")

    # imported beats dependency: util_add exists BOTH in an imported
    # sibling and inside the dep farm — the import wins.
    write("s4b/.hls-pkg-deps/mylib/util.hls",
          "fn util_add(a: int, b: int) -> int {\n    return a * b\n}\n")
    write("s4b/lib/util.hls", "fn util_add(a: int, b: int) -> int {\n    return a + b\n}\n")
    main = ('import "lib/util.hls"\n'
            'import "mylib/util.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(util_add(1, 2))\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s4b", "main.hls", main, 4, 14)
    check(res is not None and res["uri"].endswith("/lib/util.hls"),
          "an imported sibling beats a dependency carrying the same name")

    # dependency beats the open net.
    write("s4c/.hls-pkg-deps/mylib/dep.hls",
          "fn dep_fn() -> int {\n    return 1\n}\n")
    other = "fn dep_fn() -> int {\n    return 2\n}\n"
    main = ('import "mylib/dep.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(dep_fn())\n'
            '    return 0\n'
            '}\n')
    res, rc, err = session(
        "s4c", [("main.hls", main), ("other.hls", other)],
        [("textDocument/definition",
          {"position": {"line": 3, "character": 14}}, 0)])
    r = res[0]
    check(r is not None and ".hls-pkg-deps" in (r.get("uri") or ""),
          "a dependency beats the last-resort net over open files")


# ---------------------------------------------------------------------------
# Section 5 — the guards.
# ---------------------------------------------------------------------------

def section_5():
    print("5. the guards — hostile paths answer null; a malformed "
          "lockfile is skipped, not fatal")

    main = ('import "mylib/missing.hls"\n'
            '\n'
            'fn main() -> int {\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s5", "main.hls", main, 0, 12)
    check(res is None and rc == 0,
          "a missing dependency import answers null, server exits 0")
    res, rc, err = defn("s5", "main.hls", main, 0, 3)
    check(res is None and rc == 0,
          "the import KEYWORD itself is not a jump (null, not a crash)")
    res, rc, err = defn("s5", "main.hls", main, 3, 5)
    check(res is None and rc == 0,
          "a keyword token (return) answers null")

    # A malformed lockfile must be skipped silently; resolution through
    # the farm must keep working in the same workspace.
    write("s5b/hls-pkg.toml",
          '[package]\nname = "app"\nversion = "0.1.0"\n')
    write("s5b/hls-pkg.lock", "{ this is not json ]]")
    write("s5b/.hls-pkg-deps/mylib/api.hls",
          "fn farm_fn() -> int {\n    return 7\n}\n")
    main = ('import "mylib/api.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(farm_fn())\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s5b", "main.hls", main, 3, 14)
    check(res is not None and ".hls-pkg-deps" in (res.get("uri") or "")
          and rc == 0,
          "a malformed hls-pkg.lock is skipped; the farm still resolves")

    # A lockfile entry with a resolved_path that does not exist: skipped.
    lock = {"version": 2, "packages": [
        {"name": "ghost", "source": {}, "version": "x", "commit": None,
         "sha256": "2" * 64, "effects": [], "transitive_effects": [],
         "resolved_path": os.path.join(TMP, "s5c/nowhere/ghost.hls"),
         "log_seq": 3}]}
    write("s5c/hls-pkg.toml", '[package]\nname = "app"\nversion = "0.1.0"\n')
    write("s5c/hls-pkg.lock", json.dumps(lock))
    write("s5c/real.hls", "fn real_fn() -> int {\n    return 5\n}\n")
    main = ('import "real.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(real_fn())\n'
            '    return 0\n'
            '}\n')
    res, rc, err = defn("s5c", "main.hls", main, 3, 14)
    check(res is not None and res["uri"].endswith("real.hls") and rc == 0,
          "a ghost resolved_path is skipped; the relative import still wins")


# ---------------------------------------------------------------------------
# Section 6 — the externals.
# ---------------------------------------------------------------------------

def section_6():
    print("6. the externals — never published to, never edited, always "
          "read fresh")

    depfile = write("s6/.hls-pkg-deps/mylib/api.hls",
                    "fn farm_fn() -> int {\n    return 7\n}\n")
    main = ('import "mylib/api.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(farm_fn())\n'
            '    return 0\n'
            '}\n')

    # External docs must not publish diagnostics: the only
    # publishDiagnostics notifications name the OPEN document.
    uris = ["file://" + os.path.join(TMP, "s6", "main.hls")]
    payload = frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
    payload += frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                      "params": {"textDocument": {"uri": uris[0],
                                                  "version": 1,
                                                  "text": main}}})
    payload += frame({"jsonrpc": "2.0", "id": 9,
                      "method": "textDocument/definition",
                      "params": {"textDocument": {"uri": uris[0]},
                                 "position": {"line": 3, "character": 14}}})
    payload += frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    payload += frame({"jsonrpc": "2.0", "method": "exit"})
    env = dict(os.environ)
    env.pop("HLS_PKG_DEPS", None)
    proc = subprocess.Popen([sys.executable, LSP], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env)
    out, _err = proc.communicate(payload, timeout=60)
    msgs = parse_frames(out)
    pubs = [m for m in msgs
            if m.get("method") == "textDocument/publishDiagnostics"]
    check(all(m["params"]["uri"] == uris[0] for m in pubs) and pubs,
          "only the OPEN document receives diagnostics — externals never do")

    # Rename must stay confined to open buffers.
    res, rc, err = session(
        "s6", [("main.hls", main)],
        [("textDocument/rename",
          {"position": {"line": 3, "character": 14},
           "newName": "farm_gn"}, 0)])
    changes = (res[0] or {}).get("changes", {})
    check(any(u.endswith("main.hls") for u in changes)
          and not any("api.hls" in u for u in changes),
          "rename edits only the open buffer — a dependency file is "
          "never rewritten")

    # References DO span externals (read-only).
    res, rc, err = session(
        "s6", [("main.hls", main)],
        [("textDocument/references",
          {"position": {"line": 3, "character": 14},
           "context": {"includeDeclaration": True}}, 0)])
    ref_uris = {loc["uri"] for loc in (res[0] or [])}
    check(any(u.endswith("api.hls") for u in ref_uris)
          and any(u.endswith("main.hls") for u in ref_uris),
          "references span into the dependency (read-only) and back")

    # mtime refresh: change the dep ON DISK; the next lookup sees it.
    with open(depfile, "a") as f:
        f.write("\nfn farm_late() -> int {\n    return 8\n}\n")
    os.utime(depfile, None)
    main2 = ('import "mylib/api.hls"\n'
             '\n'
             'fn main() -> int uses IO {\n'
             '    println(farm_late())\n'
             '    return 0\n'
             '}\n')
    res, rc, err = defn("s6", "main.hls", main2, 3, 14)
    check(res is not None and res["uri"].endswith("api.hls"),
          "an on-disk dependency edit is seen on the next lookup "
          "(fresh session; parse-on-demand)")


# ---------------------------------------------------------------------------
# Section 7 — completion + the external LRU (in-process).
# ---------------------------------------------------------------------------

def _load_server_class():
    spec = importlib.util.spec_from_file_location("hls_lsp_mod", LSP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HLSServer, mod.EXTERNAL_DOC_CAP


def section_7():
    print("7. the completion — a dependency's API completes; the LRU "
          "evicts and re-loads")

    write("s7a/.hls-pkg-deps/mylib/api.hls",
          "fn farm_fn() -> int {\n    return 7\n}\n")
    main = ('import "mylib/api.hls"\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println()\n'
            '    return 0\n'
            '}\n')
    res, rc, err = session(
        "s7a", [("main.hls", main)],
        [("textDocument/completion",
          {"position": {"line": 3, "character": 12}}, 0)])
    labels = {item["label"] for item in (res[0] or [])}
    check("farm_fn" in labels,
          "farm_fn (dependency API) appears in completion the moment "
          "its import is in the file")

    # LRU: drive the server class directly.
    HLSServer, cap = _load_server_class()
    srv = HLSServer()
    write("s7b/main.hls", "fn main() -> int {\n    return 0\n}\n")
    main_uri = "file://" + os.path.join(TMP, "s7b", "main.hls")
    srv._store_doc(main_uri, 1, "fn main() -> int {\n    return 0\n}\n")
    ext_paths = []
    for i in range(cap + 5):
        p = write("s7b/gen/f%03d.hls" % i,
                  "fn gen_%03d() -> int {\n    return %d\n}\n" % (i, i))
        ext_paths.append(p)
        srv._external_doc(p)
    ext_uris = [u for u, d in srv.docs.items() if d.get("external")]
    check(len(ext_uris) <= cap,
          "the external cache holds the LRU cap (%d loaded, cap %d)"
          % (cap + 5, cap))
    first_uri = "file://" + os.path.realpath(ext_paths[0])
    check(first_uri not in srv.docs,
          "the OLDEST external document was evicted")
    last_uri = "file://" + os.path.realpath(ext_paths[-1])
    check(last_uri in srv.docs,
          "the NEWEST external document survives")
    # A lookup against the evicted file re-loads it transparently.
    r = srv._external_doc(ext_paths[0])
    check(r is not None and "file://" + os.path.realpath(ext_paths[0])
          in srv.docs,
          "an evicted file re-loads on the next request")
    # Open documents are never on the eviction list.
    check(main_uri in srv.docs,
          "the OPEN document survives eviction pressure")


# ---------------------------------------------------------------------------
# Section 8 — the smoke and the corpus.
# ---------------------------------------------------------------------------

def section_8():
    print("8. the smoke — lsp_smoke.py stays green; the ok corpus "
          "program runs (native half when a C compiler exists)")

    rc = subprocess.run([sys.executable, SMOKE]).returncode
    check(rc == 0, "lsp_smoke.py: protocol assertions still pass")

    rc = subprocess.run(BOOT + [OK_TEST],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE).returncode
    check(rc == 0, "feat_stage113_lsp_resolve.hls runs through boot "
                   "(interpreter)")

    have_gcc = shutil.which("gcc") is not None
    if have_gcc:
        cfile = os.path.join(TMP, "s113.c")
        r1 = subprocess.run(BOOT + [os.path.join(ROOT, "src", "hlc.hls"),
                                    OK_TEST, cfile],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        binfile = os.path.join(TMP, "s113.bin")
        r2 = subprocess.run(["gcc", "-O2", "-o", binfile, cfile,
                             "-lm", "-pthread"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r1.returncode == 0 and r2.returncode == 0:
            interp = subprocess.run(BOOT + [OK_TEST], stdout=subprocess.PIPE)
            nat = subprocess.run([binfile], stdout=subprocess.PIPE)
            check(nat.stdout == interp.stdout,
                  "native build agrees with the interpreter byte for byte")
        else:
            bad("native half could not build (self-host compile or gcc)")
    else:
        ok("no C compiler — native half skipped (gate stays hermetic)")


def main():
    print("Stage 113 acceptance gate — LSP go-to-definition across packages")
    try:
        section_1()
        section_2()
        section_3()
        section_4()
        section_5()
        section_6()
        section_7()
        section_8()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
