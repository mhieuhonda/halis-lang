"""glue_cli - verbatim segment of the original tools/hlwasm.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hwasm_common import (
    HLError, List, Optional, Tuple, _REPO_ROOT, argparse, os, subprocess,
    sys,
)
from hwasm_emit_lower import (
    HTML_RUNNER, JS_GLUE, JS_GLUE_COMPACT, JS_GLUE_COMPACT_STUBS,
)
from hwasm_wasmemitter import (
    WasmEmitter,
)

def generate_js_glue(verbose: bool = False,
                     imports: Optional[List[str]] = None) -> str:
    """Return the JS glue source.

    Stage 24: the compact glue (default) includes the struct-marshalling
    API (Halis.readStruct, Halis.writeStruct, Halis.registerStruct).
    The verbose glue (the Stage 23 version) is kept for debugging — pass
    ``verbose=True``.

    Deep-scan-23: ``imports`` is the wasm module's declared import names
    (emitter.mod.imports). The compact glue re-injects ONLY the std.jsffi
    default stubs the module actually imports, so the glue size scales
    with the program's real JS surface instead of carrying every Stage 73
    stub unconditionally. ``imports=None`` keeps every stub (used by the
    emscripten bridge, whose import section is not ours).
    """
    if verbose:
        return JS_GLUE
    if imports is None:
        wanted = set(JS_GLUE_COMPACT_STUBS)
    else:
        wanted = set(imports)
    parts = []
    for name in JS_GLUE_COMPACT_STUBS:
        if name in wanted:
            parts.append(name + ':' + JS_GLUE_COMPACT_STUBS[name])
    extra = ',\n'.join(parts) + (',' if parts else '')
    return JS_GLUE_COMPACT.replace('__EXTRA_ENV_STUBS__', extra)


def generate_html_runner(title: str, wasm_name: str, js_name: str,
                         wasm_size: int) -> str:
    return HTML_RUNNER.format(
        title=title, wasm_name=wasm_name, js_name=js_name,
        wasm_size=wasm_size)


# ============================================================================
# Orchestrator
# ============================================================================

SUPPORTED_TARGETS = {
    "wasm32-unknown-unknown": {
        "backend": "direct",
        "description": "Freestanding wasm32 (no libc; JS imports)",
    },
    "wasm32-unknown-emscripten": {
        "backend": "emscripten-or-direct",
        "description": "Emscripten libc (uses emcc if available; falls back to freestanding)",
    },
}

TARGET_ALIASES = {
    "wasm32": "wasm32-unknown-unknown",
    "wasm": "wasm32-unknown-unknown",
    "wasi": "wasm32-unknown-unknown",  # approximation; true WASI is later
    "emscripten": "wasm32-unknown-emscripten",
}


def canonical_target(name: str) -> str:
    if name in SUPPORTED_TARGETS:
        return name
    if name in TARGET_ALIASES:
        return TARGET_ALIASES[name]
    raise ValueError("unknown target '%s'. Use --list-targets." % name)


def find_emcc() -> Optional[str]:
    """Return the path to ``emcc`` if available, else None."""
    import shutil
    return shutil.which("emcc")


def compile_via_emscripten(input_hls: str, output_base: str,
                           target: str, opt_level: str) -> Optional[Tuple[bytes, str]]:
    """Stage 24 emscripten bridge: compile HLS -> C -> emcc -> wasm + js.

    Returns (wasm_bytes, js_glue_path) on success, or None if emcc is
    not available (caller should fall back to the freestanding backend).

    The emcc invocation:
      1. hlc <input.hls> <tmp.c>     (HLS -> portable ANSI C)
      2. emcc -O<level> -s WASM=1 -s ENVIRONMENT=web,node \
             -s EXPORTED_FUNCTIONS=[_main] \
             -o <output_base>.js <tmp.c>    (emcc emits both .js + .wasm)

    Deep-scan-24 fix: the export list previously requested _hl_main,
    __start and _hl_alloc — symbols the compiled C does NOT define (hlc
    emits a plain `int main(...)`, and the hl_alloc helper only exists
    in the FREESTANDING wasm backend, not in the C runtime). Newer emcc
    builds fail with "undefined exported symbol" on such names, so the
    bridge silently degraded to the freestanding fallback. Export the
    one symbol that exists (_main, the hlc entry point); ccall/cwrap in
    EXPORTED_RUNTIME_METHODS cover the JS-side marshalling needs.

    The emcc-generated .js glue replaces our compact glue — it provides
    full libc access (printf, malloc, file IO, etc.). Our compact glue
    is still written alongside as ``<output_base>.halis-glue.js`` so the
    struct-marshalling API remains available.
    """
    emcc = find_emcc()
    if emcc is None:
        return None
    hlc = os.path.join(_REPO_ROOT, "bin", "hlc")
    if not os.path.isfile(hlc):
        # Try building via the bootstrap compiler.
        boot_py = os.path.join(_REPO_ROOT, "boot", "boot.py")
        if not os.path.isfile(boot_py):
            return None
        hlc = None  # will use boot.py directly
    tmp_c = output_base + ".c"
    # Step 1: HLS -> C.
    if hlc is not None:
        cmd = [hlc, input_hls, tmp_c]
    else:
        cmd = [sys.executable, boot_py, "src/hlc.hls", input_hls, tmp_c]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write("error: hlc failed (Stage 24 emscripten bridge)\n")
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
        return None
    if not os.path.isfile(tmp_c):
        sys.stderr.write("error: hlc did not produce %s\n" % tmp_c)
        return None
    # Step 2: emcc -> wasm + js.
    o_flag = {"O1": "-O1", "O2": "-O2", "O3": "-O3", "Os": "-Os"}.get(
        opt_level, "-O2")
    js_out = output_base + ".js"
    cmd = [emcc, o_flag, "-s", "WASM=1",
           "-s", "ENVIRONMENT=web,node",
           "-s", "EXPORTED_FUNCTIONS=[_main]",
           "-s", "EXPORTED_RUNTIME_METHODS=[ccall,cwrap,UTF8ToString,stringToUTF8]",
           "-o", js_out, tmp_c, "-lm"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write("error: emcc failed (Stage 24 emscripten bridge)\n")
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[:500])
        return None
    # Read back the wasm + js.
    wasm_path = output_base + ".wasm"
    if not os.path.isfile(wasm_path):
        # emcc might emit a different name; look for the .wasm alongside.
        for fn in os.listdir(os.path.dirname(os.path.abspath(js_out)) or "."):
            if fn.endswith(".wasm"):
                wasm_path = os.path.join(
                    os.path.dirname(os.path.abspath(js_out)) or ".", fn)
                break
    with open(wasm_path, "rb") as f:
        wasm_bytes = f.read()
    try:
        os.unlink(tmp_c)
    except OSError:
        pass
    return (wasm_bytes, js_out)


def compile_program(input_hls: str, output_base: str,
                    target: str = "wasm32-unknown-unknown",
                    emit_wasm: bool = True,
                    emit_js: bool = True,
                    emit_html: bool = True,
                    run: bool = False,
                    wasm_opt: str = "auto",
                    opt_level: str = "O3",
                    glue_style: str = "compact",
                    serve: Optional[int] = None) -> int:
    """Compile an HLS program to a .wasm + .js + .html bundle.

    ``output_base`` is the base path (no extension); the output files are
    ``output_base.wasm``, ``output_base.js``, ``output_base.html``.

    Stage 24 parameters:
      ``wasm_opt``: "auto" (default; run in-tree + external if available),
                    "on" (always run in-tree; external if available),
                    "off" (no optimization).
      ``opt_level``: O1/O2/O3/Os (default O3).
      ``glue_style``: "compact" (default; ~2.5 KB) or "verbose" (~5.5 KB).
      ``serve``: if not None, start the dev server on the given port
                 after compiling (Stage 24 ``hls serve``).
    """
    target = canonical_target(target)
    if target == "wasm32-unknown-emscripten":
        # Stage 24: try the emscripten bridge first; fall back to the
        # freestanding backend if emcc is not available.
        result = compile_via_emscripten(
            input_hls, output_base, target, opt_level)
        if result is not None:
            wasm_bytes, js_path = result
            sys.stderr.write(
                "Stage 24 emscripten bridge: emcc produced %s (%d bytes)\n"
                % (js_path, len(wasm_bytes)))
            # Write the .wasm file (if emcc wrote it elsewhere, we've
            # already read it into wasm_bytes).
            if emit_wasm:
                wasm_out = output_base + ".wasm"
                if wasm_out != js_path.replace(".js", ".wasm"):
                    with open(wasm_out, "wb") as f:
                        f.write(wasm_bytes)
            # Write our compact glue alongside (for struct-marshalling).
            if emit_js:
                glue_path = output_base + ".halis-glue.js"
                with open(glue_path, "w") as f:
                    f.write(generate_js_glue(
                        verbose=(glue_style == "verbose")))
            # Run wasm-opt if requested.
            if wasm_opt != "off":
                wasm_bytes = _run_wasm_opt(wasm_bytes, opt_level, wasm_opt)
                # Rewrite the optimized wasm.
                if emit_wasm:
                    with open(output_base + ".wasm", "wb") as f:
                        f.write(wasm_bytes)
            # Optionally run.
            if run:
                rc = run_wasm_in_node(output_base + ".wasm")
                if rc != 0:
                    return rc
            if serve is not None:
                _start_dev_server(output_base, serve)
            return 0
        # Fall back to the freestanding backend.
        sys.stderr.write(
            "note: emcc not found; wasm32-unknown-emscripten falls back "
            "to the freestanding backend (install emscripten for full "
            "libc access).\n")
    # Load + check the program (mirrors boot.py's pipeline).
    sys.path.insert(0, _REPO_ROOT)
    from boot.boot import load_program  # type: ignore
    from boot.checker import check  # type: ignore
    program = load_program(input_hls)
    check(program)  # raises HLError on failure
    # Emit the wasm binary.
    emitter = WasmEmitter(program, target=target)
    wasm_bytes = emitter.emit()
    # Stage 24: run wasm-opt (in-tree + external) if requested.
    if wasm_opt != "off":
        wasm_bytes = _run_wasm_opt(wasm_bytes, opt_level, wasm_opt)
    # Write outputs.
    os.makedirs(os.path.dirname(os.path.abspath(output_base)) or ".",
                exist_ok=True)
    if emit_wasm:
        wasm_path = output_base + ".wasm"
        with open(wasm_path, "wb") as f:
            f.write(wasm_bytes)
        sys.stderr.write("wrote %s (%d bytes)\n" % (wasm_path, len(wasm_bytes)))
    if emit_js:
        js_path = output_base + ".js"
        import_names = [n for (_m, n, _k, _t) in emitter.mod.imports]
        with open(js_path, "w") as f:
            f.write(generate_js_glue(verbose=(glue_style == "verbose"),
                                     imports=import_names))
        sys.stderr.write("wrote %s (%d bytes)\n" % (js_path, os.path.getsize(js_path)))
    if emit_html:
        html_path = output_base + ".html"
        wasm_name = os.path.basename(output_base) + ".wasm"
        js_name = os.path.basename(output_base) + ".js"
        with open(html_path, "w") as f:
            f.write(generate_html_runner(
                title="Halis: %s" % os.path.basename(input_hls),
                wasm_name=wasm_name, js_name=js_name,
                wasm_size=len(wasm_bytes)))
        sys.stderr.write("wrote %s\n" % html_path)
    # Optionally run.
    if run:
        rc = run_wasm_in_node(output_base + ".wasm")
        if rc != 0:
            return rc
    if serve is not None:
        _start_dev_server(output_base, serve)
    return 0


def _run_wasm_opt(wasm_bytes: bytes, opt_level: str,
                  wasm_opt_mode: str) -> bytes:
    """Run the in-tree wasm optimizer on ``wasm_bytes`` and (if available)
    the external ``wasm-opt`` binary. Returns the optimized bytes.

    The in-tree optimizer (``tools/hlwasm_opt.py``) performs dead function
    elimination, dead import elimination, type-section deduplication,
    local compaction, dead data elimination, and peephole opts. The
    external ``wasm-opt`` (Binaryen) performs binary-level passes
    (inlining, alias analysis, etc.) that go beyond the in-tree scope.
    """
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "tools"))
        from hlwasm_opt import optimize as _opt  # type: ignore
    except ImportError:
        sys.stderr.write("warning: hlwasm_opt not found; skipping optimization\n")
        return wasm_bytes
    report: dict = {}
    optimized = _opt(wasm_bytes, level=opt_level, report=report)
    if report.get("bytes_saved", 0) > 0:
        sys.stderr.write(
            "wasm-opt: %d -> %d bytes (-%d, %.1f%%)\n"
            % (report["input_size"], report["output_size"],
               report["bytes_saved"], report["reduction_pct"]))
    return optimized


def _start_dev_server(output_base: str, port: int) -> None:
    """Start the Stage 24 ``hls serve`` dev server (delegates to
    tools/hlserve.py). The server watches the cwd for .hls changes and
    re-compiles the wasm bundle on save."""
    hlserve = os.path.join(_REPO_ROOT, "tools", "hlserve.py")
    if not os.path.isfile(hlserve):
        sys.stderr.write("warning: tools/hlserve.py not found; "
                         "cannot start dev server\n")
        return
    cmd = [sys.executable, hlserve, "--port", str(port),
           "--bundle", output_base]
    sys.stderr.write("starting dev server on port %d...\n" % port)
    # Detach: the server runs in the foreground (Ctrl+C to stop).
    os.execv(sys.executable, cmd)



def run_wasm_in_node(wasm_path: str) -> int:
    """Run the compiled wasm in Node.js (if available). Returns the exit
    code."""
    node = shutil_which("node")
    if node is None:
        sys.stderr.write("note: --run requested but node.js is not installed; "
                         "skipping execution.\n")
        return 0
    # Write a small runner script. The extension is .cjs, NOT .js: Node
    # decides a file's module system from the NEAREST package.json, so a
    # `.run.js` under a directory whose package.json says
    # `"type": "module"` is parsed as an ES module — where `require` does
    # not exist and the runner dies before it ever reaches the wasm. A
    # generated helper that Node executes must not be at the mercy of a
    # manifest three directories up.
    runner = wasm_path + ".run.cjs"
    with open(runner, "w") as f:
        f.write(NODE_RUNNER_TEMPLATE % {
            "wasm_path": wasm_path,
        })
    try:
        result = subprocess.run([node, runner], capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    finally:
        try:
            os.unlink(runner)
        except OSError:
            pass


NODE_RUNNER_TEMPLATE = r"""
const fs = require("fs");
const path = require("path");
const wasmBytes = fs.readFileSync("%(wasm_path)s");
// Inline the glue (the .js file is alongside the .wasm).
const gluePath = "%(wasm_path)s".replace(/\.wasm$/, ".js");
if (fs.existsSync(gluePath)) {
  // Load the glue into this scope.
  const src = fs.readFileSync(gluePath, "utf-8");
  eval(src);
} else {
  console.error("glue file not found: " + gluePath);
  process.exit(1);
}
Halis.run(new Uint8Array(wasmBytes)).then(function (code) {
  // exit code (i64) — Node's process.exit takes int32, clamp.
  process.exit(Number(code) & 0x7fffffff);
}).catch(function (e) {
  console.error("Halis run failed: " + e.message);
  process.exit(1);
});
"""


def shutil_which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


# Repo root for resolving imports (defined at module top — re-aliased
# here for clarity to readers grepping for it).


def cmd_list_targets() -> int:
    print("Supported WebAssembly targets (Stage 24):")
    print()
    for triple, spec in SUPPORTED_TARGETS.items():
        print("  %s" % triple)
        print("      %s" % spec["description"])
    print()
    print("Aliases (accepted by --target):")
    for alias, canonical in TARGET_ALIASES.items():
        print("  %-16s -> %s" % (alias, canonical))
    print()
    emcc = find_emcc()
    if emcc:
        print("emcc found at: %s" % emcc)
        print("  (full emscripten libc access is available for "
              "wasm32-unknown-emscripten)")
    else:
        print("emcc: not found (wasm32-unknown-emscripten falls back to "
              "the freestanding backend)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 24 WebAssembly backend: HLS -> .wasm + wasm-opt + emscripten bridge.")
    ap.add_argument("input", nargs="?",
                    help="HLS source file (e.g. examples/hello.hls)")
    ap.add_argument("output_base", nargs="?",
                    help="output base path (no extension); "
                         "writes .wasm + .js + .html")
    ap.add_argument("--target", default="wasm32-unknown-unknown",
                    help="target triple (default: wasm32-unknown-unknown)")
    ap.add_argument("--no-wasm", action="store_true",
                    help="don't write the .wasm file")
    ap.add_argument("--no-js", action="store_true",
                    help="don't write the .js glue file")
    ap.add_argument("--no-html", action="store_true",
                    help="don't write the .html runner")
    ap.add_argument("--run", action="store_true",
                    help="run the compiled wasm in Node.js (if available)")
    ap.add_argument("--list-targets", action="store_true",
                    help="list the supported target triples and exit")
    ap.add_argument("--wasm-opt", default="auto",
                    choices=["auto", "on", "off"],
                    help="Stage 24: run the wasm size optimizer (default: "
                         "auto = run in-tree + external if available)")
    ap.add_argument("--opt-level", default="O3",
                    choices=["O1", "O2", "O3", "Os"],
                    help="Stage 24: optimization level (default: O3)")
    ap.add_argument("--glue", default="compact",
                    choices=["compact", "verbose"],
                    help="Stage 24: JS glue style (default: compact ~2.5 KB; "
                         "verbose ~5.5 KB)")
    ap.add_argument("--serve", type=int, default=None, metavar="PORT",
                    help="Stage 24: after compiling, start the dev server "
                         "(hls serve) on PORT (watches .hls files, "
                         "recompiles on save, live-reload via SSE)")
    args = ap.parse_args()
    if args.list_targets:
        return cmd_list_targets()
    if not args.input or not args.output_base:
        ap.error("input and output_base are required "
                 "(or use --list-targets)")
    try:
        return compile_program(
            args.input, args.output_base,
            target=args.target,
            emit_wasm=not args.no_wasm,
            emit_js=not args.no_js,
            emit_html=not args.no_html,
            run=args.run,
            wasm_opt=args.wasm_opt,
            opt_level=args.opt_level,
            glue_style=args.glue,
            serve=args.serve)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1
    except ValueError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 2




__all__ = [
    "NODE_RUNNER_TEMPLATE",
    "SUPPORTED_TARGETS",
    "TARGET_ALIASES",
    "_run_wasm_opt",
    "_start_dev_server",
    "canonical_target",
    "cmd_list_targets",
    "compile_program",
    "compile_via_emscripten",
    "find_emcc",
    "generate_html_runner",
    "generate_js_glue",
    "main",
    "run_wasm_in_node",
    "shutil_which",
]
