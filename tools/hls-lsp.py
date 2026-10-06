#!/usr/bin/env python3
"""hls-lsp — Language Server for Halis (HLS).

Stage 14 release (v0.24.0-alpha): cross-file go-to-definition with
import resolution, rename refactoring, document symbols, document
highlight, and signature-info. Editor plugins (VS Code, Neovim) ship
under editors/.

Stage 113 (v0.132.0-alpha): go-to-definition ACROSS PACKAGES. Until
now a definition was only ever found inside an OPEN document, so a
symbol that lived in a dependency (or in std/, or in a sibling file
nobody had opened) resolved to nothing. The server now resolves
definitions through four on-disk layers, in priority order:

  1. the files the current document imports (open buffers win over
     disk; unopened files are parsed on demand),
  2. the package sources named by hls-pkg.lock's `resolved_path`
     entries — the same file the compiler builds from,
  3. the `.hls-pkg-deps/` symlink farm `hls-pkg build` maintains,
     walked from the document's directory chain and the package root,
  4. the `HLS_PKG_DEPS` directory boot.py itself honours — the server
     must never disagree with the build about where code lives.

An `import "..."` path literal is itself jumpable: the definition of
an import is the module file it names. On-disk files are loaded as
EXTERNAL documents — parsed once, mtime-checked, LRU-capped — which
answer definitions/references/completion like open buffers but are
NEVER edited (rename stays confined to opened files) and never
publish diagnostics. Malformed or missing dependency files answer
None; they can never take the server down.

Stage 114 (v0.133.0-alpha): textDocument/inlayHint — inlay hints for
PARAMETER NAMES and BINDING TYPES. Two hint families, one discipline:
never guess. A wrong hint is worse than no hint.

  - Parameter-name hints sit before each argument at a call site
    (`write_file(‹path:› p, ‹content:› body)`), resolved in checker
    order: curated builtins (SPEC §8 names, arities re-verified against
    the checker) → local fns → the files the document imports (the
    Stage 113 layers) — with methods resolved through a
    unique-candidate rule that SKIPS the ambiguous cases (two structs
    defining the same method name; a user method shadowing a builtin
    method; `Enum.Variant(...)` constructors whose payloads have no
    names; variadic builtins the spec does not pin).

  - Type hints sit after each match-arm payload binding
    (`Color.RGB(r‹: int›, g‹: int›, b‹: int›)`) — the only bindings in
    the language written without an annotation (let and for both
    require one). Types come from the checker's own instantiation when
    it ran (generic enums show `int`, not `T`), falling back to the
    raw variant payloads when the checker did not reach the arm.

  - A hint is emitted only when the argument count EXACTLY matches
    the parameter count, the call site was located in the token
    stream (an in-order AST walk correlated with a monotonic token
    cursor — the AST carries no columns), and the argument is not a
    bare identifier equal to the parameter name (`f(x: x)` says
    nothing the source does not).

Stage 14-alpha (v0.12.0-alpha): minimal LSP server over JSON-RPC stdio.

Implemented methods:
  initialize / shutdown / exit
  textDocument/didOpen / didChange / didClose (full document sync)
  textDocument/hover        — show the inferred type of an identifier at
                              a position (uses the checker's annotations).
  textDocument/definition   — find the function/struct/enum definition at
                              a position: the current file, the files it
                              imports (open or on disk), the workspace's
                              package dependency sources (hls-pkg.lock
                              resolved_path, .hls-pkg-deps/, HLS_PKG_DEPS),
                              then every other open document. An import
                              path literal jumps to the module file itself
                              (cross-package go-to-definition, Stage 113).
  textDocument/references   — find all references to the symbol at a
                              position (used by rename preflight).
  textDocument/rename      — rename the symbol at a position across
                              all open documents (Stage 14 release target).
  textDocument/documentSymbol — list every top-level fn/struct/enum in
                              the file (used by VS Code's outline view).
  textDocument/completion   — basic keyword + identifier completion.
  textDocument/inlayHint    — parameter names at call sites and types on
                              match-arm payload bindings (Stage 114);
                              signatures resolve through the same layer
                              discipline as definitions, and every guard
                              degrades to "no hint", never to a wrong one.
  textDocument/publishDiagnostics (notification) — runs the Stage-0
                              checker and publishes errors as diagnostics.

Usage:
  hls-lsp                    # start the server on stdio
  hls-lsp --check FILE.hls   # one-shot: print diagnostics to stdout
                              (useful for editors that don't speak LSP)

Status: Stage 114. The LSP server uses the Stage-0 lexer/parser/checker
internally; go-to-definition resolves through the same import rules
boot.py enforces, extended with the package layers (lockfile,
.hls-pkg-deps/, HLS_PKG_DEPS), and inlay hints resolve signatures
through the same layer discipline. Editor plugins live under
editors/vscode/ and editors/neovim/.
"""
import argparse
import json
import os
import sys
import traceback
from urllib.parse import quote, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from boot.lexer import tokenize, HLError  # noqa: E402
from boot.parser import Parser  # noqa: E402
from boot.checker import check  # noqa: E402


# ---------------------------------------------------------------------------
# JSON-RPC over stdio.
# ---------------------------------------------------------------------------

def read_message():
    """Read a single JSON-RPC message from stdin (Content-Length framing).

    BUG-DS4-16: this used to RAISE on a malformed frame (bad JSON, bad
    Content-Length header, short read at EOF) — and since run() called it
    OUTSIDE its try/except, one malformed frame killed the whole server.
    The LSP spec requires a -32700 Parse error response and staying alive.
    Now returns ("__parse_error__", detail) markers instead of raising;
    returns None only at clean EOF.
    """
    headers = {}
    try:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
    except OSError as ex:
        return ("__parse_error__", "stdin read error: %s" % ex)
    if b"content-length" not in headers:
        # No body framing — treat as a parse error (the client is speaking
        # something other than LSP framing).
        return ("__parse_error__", "missing Content-Length header")
    try:
        n = int(headers[b"content-length"])
    except ValueError:
        return ("__parse_error__", "invalid Content-Length: %r"
                % headers[b"content-length"])
    if n < 0 or n > (1 << 28):
        return ("__parse_error__", "unreasonable Content-Length: %d" % n)
    body = sys.stdin.buffer.read(n)
    if len(body) != n:
        return ("__parse_error__", "short read: wanted %d bytes, got %d"
                % (n, len(body)))
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as ex:
        return ("__parse_error__", "invalid JSON body: %s" % ex)


def write_message(msg):
    """Write a JSON-RPC message to stdout with Content-Length framing."""
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n" % len(body))
    sys.stdout.buffer.write(b"\r\n")
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# HLS LSP server.
# ---------------------------------------------------------------------------

KEYWORDS = ["fn", "let", "mut", "return", "if", "else", "while", "for", "in",
            "break", "continue", "struct", "impl", "import", "uses", "true",
            "false", "enum", "match", "pure", "extern"]
# Deep-scan-7 fix: include ALL Stage 9/10 release builtins. The Stage
# 14-alpha list was stale — missing read_line, net_lookup, rand_int,
# rand_float, rand_seed, proc_exec. Editors offered auto-completion for
# only the original 22 builtins.
BUILTINS = [
    # Original builtins
    "println", "print", "len", "str", "int", "panic",
    "clock_ms", "args", "exit", "chr", "range", "map_new",
    "drop", "clone", "take", "file_exists", "read_file",
    "write_file", "tainted_args", "taint_mark", "taint_unwrap",
    "read_file_tainted",
    # Stage 9 release builtins
    "net_lookup", "rand_int", "rand_float", "rand_seed",
    "proc_exec", "read_line",
    # Stage 9-beta struct default helpers
    "result_unwrap", "result_is_ok", "result_is_err",
    "option_unwrap", "option_is_some", "option_is_none",
    "map_get", "map_get_or", "map_set", "map_keys", "map_values",
    "map_contains", "map_size",
    "list_push", "list_pop", "list_get", "list_set",
    "list_size", "list_contains", "list_index_of",
    "str_to_float", "str_to_int", "str_slice", "str_concat",
    "str_contains", "str_starts_with", "str_ends_with",
    "str_split", "str_find", "str_replace", "str_upper",
    "str_lower", "str_trim", "str_bytes", "str_repeat",
    "int_to_str", "int_to_float", "float_to_int",
    "float_to_str", "bool_to_str",
]
# Deep-scan fix (C5): include Net, Rand, Proc (Stage 9 release) so editor
# autocompletion offers them when the user types `uses `.
EFFECTS = ["IO", "Fs", "Clock", "Args", "Exit", "Net", "Rand", "Proc"]

# Stage 113: the checkout the server itself lives in. When the edited
# file belongs to no package, `import "std."` still has to land in the
# toolchain's own std/ — the same fallback boot.py uses via _REPO_ROOT.
_TOOLCHAIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Stage 113: external (on-disk, unopened) documents are LRU-capped so a
# huge dependency tree cannot grow the server without bound, and capped
# per dependency source directory so a vendored monorepo cannot turn one
# definition request into a whole-repo parse.
EXTERNAL_DOC_CAP = 48
DEP_FILE_CAP = 128

# Stage 114: curated builtin parameter names — the SPEC §8 spellings,
# every arity re-verified against the checker's need(N) checks in
# boot/checking/call.py. Deliberately CLOSED: variadic and rewritten
# builtins (spawn, async_spawn, select, ...) and any builtin whose
# parameter names the spec does not pin are absent — a call to them
# gets no hints. Resolution mirrors the checker's own order (builtin
# table first, then user fns), so a user fn named `print` behaves in
# the hints exactly as it behaves in the checker.
BUILTIN_PARAM_NAMES = {
    "print": ["s"],
    "println": ["s"],
    "panic": ["msg"],
    "exit": ["code"],
    "str": ["value"],
    "int": ["s"],
    "len": ["value"],
    "range": ["a", "b"],
    "read_file": ["path"],
    "read_file_tainted": ["path"],
    "write_file": ["path", "content"],
    "file_exists": ["path"],
    "chr": ["i"],
    "rand_int": ["max"],
    "rand_seed": ["s"],
    "proc_exec": ["cmd"],
    "net_lookup": ["host"],
    "join": ["parts", "sep"],
    "has_feature": ["name"],
    "simd_cpu_supports": ["name"],
    "taint_mark": ["value"],
    "taint_unwrap": ["value"],
    "drop": ["value"],
    "clone": ["value"],
    "take": ["value"],
}

# Stage 114: curated builtin METHOD parameter names (receiver
# excluded). A name carried by more than one receiver family —
# list.set(index, value) vs map.set(key, value) — is mapped to None:
# the receiver's type is not tracked at hint time, so a guess could
# name the argument wrong. `None` and absence mean the same thing:
# no hints for that method.
BUILTIN_METHOD_PARAM_NAMES = {
    "byte_at": ["i"],
    "slice": ["a", "b"],
    "find": ["sub"],
    "contains": ["sub"],
    "starts_with": ["p"],
    "ends_with": ["p"],
    "split": ["sep"],
    "push": ["value"],
    "get": ["index"],
    "set": None,  # ambiguous: list.set(index, value) vs map.set(key, value)
    "get_or": ["key", "default"],
    "has": ["key"],
    "send": ["value"],
    "try_send": ["value"],
    "recv_or": ["default"],
}


class HLSServer:
    def __init__(self):
        # Map of uri -> {"version": int, "text": str, "program": dict}
        self.docs = {}
        self.shutdown_requested = False
        # Stage 14 release: a symbol index across all open documents for
        # cross-file go-to-definition / rename. Built lazily and
        # invalidated on every didChange. Maps
        #   symbol_name -> [(uri, line, col, kind), ...]
        # where kind is "fn", "struct", "enum", "method", "field",
        # "param", "let".
        self._symbol_cache = None
        # Stage 14 release: import-path -> uri map. Lets the server map an
        # `import "std.str"` to the open document that provides it.
        self._import_cache = None
        # Stage 113: doc directory -> package root (nearest ancestor with
        # hls-pkg.toml), cached because every definition request re-asks.
        self._ws_cache = {}
        # Stage 113: LRU order of externally loaded documents (file://
        # URIs). Open documents are never on this list — eviction checks
        # the external flag before dropping anything.
        self._ext_lru = []

    @property
    def symbol_index(self):
        if self._symbol_cache is None:
            self._rebuild_symbol_index()
        return self._symbol_cache

    @property
    def import_map(self):
        if self._import_cache is None:
            self._rebuild_symbol_index()
        return self._import_cache

    def _invalidate_indexes(self):
        self._symbol_cache = None
        self._import_cache = None
        # Stage 113: a package root can appear/disappear (hls-pkg init)
        # mid-session — drop the workspace-root cache with the rest.
        self._ws_cache = {}

    def _rebuild_symbol_index(self):
        """Rebuild the cross-file symbol index from every open doc.

        For each parsed doc we record:
          - every function (full name including Struct.method)
          - every struct + struct field
          - every enum + every enum variant
          - every let/param at function level (best-effort)
        plus the doc's `import "path"` statements so cross-file
        go-to-definition can jump to the imported file when it's open.
        """
        index = {}
        imports = {}
        for uri, doc in self.docs.items():
            prog = self._doc_program(doc)
            if prog is None:
                continue
            # Functions.
            for fname, fn in prog["fns"].items():
                index.setdefault(fname, []).append(
                    (uri, fn.get("line", 0), 0, "fn"))
                # Params as a separate, lower-priority symbol entry.
                for (pname, _ptype, _pmut) in fn.get("params", []):
                    index.setdefault(pname, []).append(
                        (uri, fn.get("line", 0), 0, "param"))
            # Structs + fields.
            for sname, sdef in prog["structs"].items():
                index.setdefault(sname, []).append(
                    (uri, sdef.get("line", 0), 0, "struct"))
                for (fname, _ftype, _dflt) in sdef.get("fields", []):
                    index.setdefault(fname, []).append(
                        (uri, sdef.get("line", 0), 0, "field"))
            # Enums + variants.
            for ename, edef in prog["enums"].items():
                index.setdefault(ename, []).append(
                    (uri, edef.get("line", 0), 0, "enum"))
                for variant in edef.get("variants", []):
                    vname = variant[0] if isinstance(variant, (list, tuple)) else variant
                    index.setdefault(vname, []).append(
                        (uri, edef.get("line", 0), 0, "variant"))
            # Imports — map import path -> uri (if the file is open).
            for imp in prog.get("imports", []):
                imports.setdefault(imp.get("path", ""), []).append(uri)
        self._symbol_cache = index
        self._import_cache = imports

    # ---------- Stage 113: package-aware definition plumbing ----------

    @staticmethod
    def _uri_to_path(uri):
        """file:// URI -> absolute filesystem path (percent-decoded), or
        None for anything that is not a file URI. Untitled buffers and
        exotic schemes cannot be indexed — there is no file to resolve."""
        if not isinstance(uri, str) or not uri.startswith("file://"):
            return None
        path = unquote(urlparse(uri).path)
        if not path:
            return None
        return os.path.normpath(path)

    @staticmethod
    def _path_to_uri(path):
        """Absolute filesystem path -> file:// URI (percent-encoded)."""
        return "file://" + quote(os.path.abspath(path))

    def _token_at(self, uri, line, col):
        """Re-tokenise the document and return the token whose extent
        covers (line, col) — 1-indexed BYTE coordinates, the lexer's own.

        Stage 113: extracted from _ident_at so the definition handler can
        also SEE string tokens — an `import "..."` path literal is a str
        token, not an ident, and its definition is the module file it
        names. The token itself is returned; the caller decides what it
        means. Same token-extent rules as before (the lexer's `raw`
        substring where present, byte length otherwise).
        """
        doc = None
        if uri is not None and uri in self.docs:
            doc = self.docs[uri]
        else:
            for _u, d in self.docs.items():
                doc = d
                break
        if not doc:
            return None
        try:
            toks = tokenize(doc["text"].encode("utf-8"))
        except HLError:
            return None
        for t in toks:
            if t["k"] == "eof":
                break
            if "raw" in t and isinstance(t["raw"], (str, bytes)):
                tlen = len(t["raw"])
            elif isinstance(t["v"], bytes):
                tlen = len(t["v"])
            else:
                tlen = len(str(t["v"]))
            if t["line"] == line and t["col"] <= col < t["col"] + tlen:
                return t
        return None

    @staticmethod
    def _import_token_path(prog, line, tok):
        """If `tok` (a str token on 1-indexed `line`) is the path literal
        of an `import "..."` statement, return the path text; else None.

        The parser records each import's keyword line, so matching on
        (line, exact path text) is exact — including two imports that
        share a line.
        """
        if tok is None or tok.get("k") != "str":
            return None
        v = tok.get("v")
        try:
            tval = v.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):
            return None
        for imp in prog.get("imports", []):
            if imp.get("line") == line and imp.get("path") == tval:
                return tval
        return None

    def _workspace_root(self, doc_path):
        """Nearest ancestor directory of `doc_path` containing
        hls-pkg.toml — the package root — or None when the file lives
        outside any package. Cached per document directory (every
        definition request re-asks); the cache is cleared by
        _invalidate_indexes so `hls-pkg init` mid-session is seen."""
        if not doc_path:
            return None
        start = os.path.dirname(os.path.abspath(doc_path))
        if start in self._ws_cache:
            return self._ws_cache[start]
        root = None
        d = start
        for _ in range(32):
            if os.path.isfile(os.path.join(d, "hls-pkg.toml")):
                root = d
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        self._ws_cache[start] = root
        return root

    def _dep_source_dirs(self, doc_path):
        """Every directory that can hold package dependency sources for
        `doc_path`, most specific first:

          - hls-pkg.lock `resolved_path` entries (a file contributes its
            directory, a directory itself) — the lockfile is what
            `hls-pkg lock` writes and `hls-pkg build` compiles from, so
            the editor must agree with the build;
          - `.hls-pkg-deps/` along the document's ancestor chain (up to
            5 levels) and at the package root — the symlink farm
            `hls-pkg build` maintains;
          - the `HLS_PKG_DEPS` directory, when set — boot.py honours it,
            so the server must too.

        Deduplicated (realpath), order-stable. Malformed lockfiles are
        skipped, never raised.
        """
        dirs = []
        seen = set()

        def add(d):
            if d and os.path.isdir(d):
                real = os.path.realpath(d)
                if real not in seen:
                    seen.add(real)
                    dirs.append(d)

        ws = self._workspace_root(doc_path) if doc_path else None
        if ws:
            lock = os.path.join(ws, "hls-pkg.lock")
            if os.path.isfile(lock):
                try:
                    with open(lock, "rb") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = None
                for pkg in (data or {}).get("packages", []):
                    if not isinstance(pkg, dict):
                        continue
                    rp = pkg.get("resolved_path")
                    if not isinstance(rp, str) or not rp:
                        continue
                    if not os.path.isabs(rp):
                        rp = os.path.join(ws, rp)
                    rp = os.path.normpath(rp)
                    if os.path.isdir(rp):
                        add(rp)
                    elif os.path.isfile(rp):
                        add(os.path.dirname(rp))
        if doc_path:
            d = os.path.dirname(os.path.abspath(doc_path))
            for _ in range(5):
                add(os.path.join(d, ".hls-pkg-deps"))
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
        env_dir = os.environ.get("HLS_PKG_DEPS")
        if env_dir:
            add(env_dir)
        return dirs

    def _resolve_import(self, import_path, importing_path):
        """Resolve an import path to an absolute file — the LSP mirror of
        boot.boot._resolve_import, minus the exits. A language server
        must answer None on a hostile path, never die on one, so the
        traversal guards (no absolute paths, no `..` segments) REJECT
        instead of raising. Same resolution families as the compiler:

          - "std.<name>" / "core.<name>": walk up from the importing
            file (5 levels), then the package root, then the toolchain's
            own std//core/ — boot.py's walk-up + _REPO_ROOT fallback,
            plus the package root for vendored module families.
          - relative paths: against the importing file's directory,
            `..`-free (boot's deep-scan-19 rule).
          - package dependency sources: hls-pkg.lock resolved_path dirs,
            .hls-pkg-deps/ farms, HLS_PKG_DEPS (boot's BUG-DS4-26
            fallback) — tried as `<dep>/<path>` and `<dep>/<basename>`,
            the same two shapes boot tries.
        """
        if not isinstance(import_path, str) or not import_path:
            return None
        for prefix, subdir in (("std.", "std"), ("core.", "core")):
            if not import_path.startswith(prefix):
                continue
            module_name = import_path[len(prefix):]
            if (not module_name
                    or module_name.startswith("/") or module_name.startswith("\\")
                    or ".." in module_name.replace("\\", "/").split("/")):
                return None
            rel = os.path.join(subdir, module_name + ".hls")
            d = (os.path.dirname(os.path.abspath(importing_path))
                 if importing_path else _TOOLCHAIN_ROOT)
            for _ in range(5):
                cand = os.path.join(d, rel)
                if os.path.isfile(cand):
                    return cand
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
            ws = self._workspace_root(importing_path) if importing_path else None
            for fallback in (ws, _TOOLCHAIN_ROOT):
                if fallback:
                    cand = os.path.join(fallback, rel)
                    if os.path.isfile(cand):
                        return cand
            return None
        if os.path.isabs(import_path):
            return None
        if ".." in import_path.replace("\\", "/").split("/"):
            return None
        base = (os.path.dirname(os.path.abspath(importing_path))
                if importing_path else os.getcwd())
        cand = os.path.normpath(os.path.join(base, import_path))
        if os.path.isfile(cand):
            return cand
        for dep_dir in self._dep_source_dirs(importing_path):
            for cand in (os.path.join(dep_dir, import_path),
                         os.path.join(dep_dir,
                                      os.path.basename(import_path))):
                if os.path.isfile(cand):
                    return cand
        return None

    def _external_doc(self, path):
        """Load (and cache) an on-disk file as an EXTERNAL document.

        External docs answer definitions/references/completion exactly
        like open buffers, but they are NEVER edited (rename skips them
        — the server will not rewrite a file the user has not opened)
        and never publish diagnostics (the checker runs once, silently,
        for indexing). Entries are mtime-checked (an on-disk change
        re-parses on the next lookup) and LRU-capped at EXTERNAL_DOC_CAP
        so a large dependency tree cannot grow the server unbounded.

        Returns the doc dict, or None when the file is missing or does
        not parse — a broken dependency file answers None, it never
        takes the server down, and failed loads are not cached (a file
        mid-write may parse on the next request).
        """
        if not path or not os.path.isfile(path):
            return None
        real = os.path.realpath(path)
        try:
            mtime = os.path.getmtime(real)
        except OSError:
            return None
        uri = self._path_to_uri(real)
        doc = self.docs.get(uri)
        if doc is not None and doc.get("external") and doc.get("mtime") == mtime:
            if uri in self._ext_lru:
                self._ext_lru.remove(uri)
                self._ext_lru.append(uri)
            return doc
        try:
            with open(real, "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
            toks = tokenize(text.encode("utf-8"))
            program = Parser(toks).parse_program()
            try:
                check(program)
            except HLError:
                pass  # index the program even when the checker objects
        except HLError:
            return None
        except (MemoryError, RecursionError, OSError):
            return None
        doc = {"version": None, "text": text, "program": program,
               "external": True, "mtime": mtime}
        self.docs[uri] = doc
        if uri in self._ext_lru:
            self._ext_lru.remove(uri)
        self._ext_lru.append(uri)
        while len(self._ext_lru) > EXTERNAL_DOC_CAP:
            old = self._ext_lru.pop(0)
            od = self.docs.get(old)
            if od is not None and od.get("external"):
                del self.docs[old]
                self._symbol_cache = None
                self._import_cache = None
        return doc

    def _import_uris(self, uri):
        """URIs (in import order) of the files `uri` imports. Open
        buffers win over disk; files not yet loaded are parsed on
        demand. Unresolvable imports are skipped, not errors."""
        doc = self.docs.get(uri)
        prog = self._doc_program(doc) if doc else None
        if prog is None:
            return []
        path = self._uri_to_path(uri)
        out = []
        for imp in prog.get("imports", []):
            target = self._resolve_import(imp.get("path", ""), path)
            if target is None:
                continue
            turi = self._path_to_uri(target)
            if turi not in self.docs:
                self._external_doc(target)
            out.append(turi)
        return out

    def _dep_file_list(self, uri):
        """On-disk .hls files from `uri`'s package dependency sources —
        the bounded walk that backs cross-package definition lookups:
        DEP_FILE_CAP files per source directory, 6 directory levels
        deep, dot-directories and __pycache__ skipped."""
        path = self._uri_to_path(uri)
        files = []
        seen = set()
        for d in self._dep_source_dirs(path):
            real = os.path.realpath(d)
            if real in seen:
                continue
            seen.add(real)
            if os.path.isfile(d):
                if d.endswith(".hls"):
                    files.append(d)
                continue
            base_depth = len(real.rstrip(os.sep).split(os.sep))
            for root, dirnames, filenames in os.walk(real):
                depth = len(root.rstrip(os.sep).split(os.sep)) - base_depth
                if depth >= 6:
                    dirnames[:] = []
                dirnames[:] = sorted(
                    x for x in dirnames
                    if not x.startswith(".") and x != "__pycache__")
                for fn in sorted(filenames):
                    if fn.endswith(".hls"):
                        files.append(os.path.join(root, fn))
                if len(files) >= DEP_FILE_CAP:
                    break
            if len(files) >= DEP_FILE_CAP:
                break
        return files[:DEP_FILE_CAP]

    def _location_for_import(self, import_path, importing_uri):
        """Definition of an `import "..."` literal = the module file it
        names. Warms the external cache so the client's follow-up
        request (document symbols, another jump) is instant. Returns an
        LSP Location or None."""
        target = self._resolve_import(
            import_path, self._uri_to_path(importing_uri))
        if target is None:
            return None
        self._external_doc(target)
        return {
            "uri": self._path_to_uri(target),
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 1}},
        }

    def run(self):
        # BUG-SC-LSP-13 fix: per LSP spec, the server must keep the connection
        # open after `shutdown` (only `exit` terminates the process). Previously
        # the loop condition `while not self.shutdown_requested` caused the
        # server to exit immediately after `shutdown`, before `exit` arrived —
        # breaking the protocol for well-behaved clients (VS Code, Neovim).
        while True:
            msg = read_message()
            if msg is None:
                break  # clean EOF
            if isinstance(msg, tuple) and msg and msg[0] == "__parse_error__":
                # BUG-DS4-16: reply -32700 Parse error (id null per JSON-RPC)
                # and keep serving.
                self.send_response(None, None, error_code=-32700,
                                   error_message="Parse error: %s" % msg[1])
                continue
            try:
                self.handle(msg)
            except Exception as ex:
                sys.stderr.write("error handling message: %s\n" % ex)
                traceback.print_exc(file=sys.stderr)
                # BUG-DS4-17: if the failed message was a REQUEST (has an id),
                # the client is waiting for a response — swallowing the
                # exception without answering hung every editor request.
                # Answer with -32603 Internal error.
                if isinstance(msg, dict) and msg.get("id") is not None:
                    try:
                        self.send_response(msg["id"], None, error_code=-32603,
                                           error_message="internal error: %s" % ex)
                    except Exception:
                        pass

    def handle(self, msg):
        method = msg.get("method")
        params = msg.get("params", {})
        msg_id = msg.get("id")
        # Deep-scan-15 fix (LOW severity, LSP spec compliance): per the
        # LSP base protocol, after a `shutdown` request the server must
        # NOT process any requests except `exit` — all other requests
        # should be rejected with error code -32600 (InvalidRequest).
        # The previous implementation processed every method regardless
        # of state; a misbehaving client could keep the server busy
        # after shutdown.
        if self.shutdown_requested and method != "exit":
            if msg_id is not None:
                self.send_response(msg_id, None, error_code=-32600,
                                   error_message="server is shutting down")
            return
        if method == "initialize":
            self.send_response(msg_id, {
                "capabilities": {
                    "textDocumentSync": 1,  # full document sync
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "renameProvider": True,
                    "documentSymbolProvider": True,
                    "completionProvider": {"triggerCharacters": [".", ":"]},
                    # Stage 114: parameter names at call sites, binding
                    # types on match-arm payloads.
                    "inlayHintProvider": True,
                },
                "serverInfo": {
                    "name": "hls-lsp",
                    "version": "0.133.0-alpha",
                },
            })
        elif method == "initialized":
            pass  # no-op
        elif method == "shutdown":
            self.shutdown_requested = True
            self.send_response(msg_id, None)
        elif method == "exit":
            # BUG-SC-LSP-20 fix: per LSP spec, `exit` should return exit code 1
            # if `shutdown` was not previously received, and 0 only if it was.
            sys.exit(0 if self.shutdown_requested else 1)
        elif method == "textDocument/didOpen":
            self.handle_did_open(params)
        elif method == "textDocument/didChange":
            self.handle_did_change(params)
        elif method == "textDocument/didClose":
            self.handle_did_close(params)
        elif method == "textDocument/hover":
            self.handle_hover(params, msg_id)
        elif method == "textDocument/definition":
            self.handle_definition(params, msg_id)
        elif method == "textDocument/references":
            self.handle_references(params, msg_id)
        elif method == "textDocument/rename":
            self.handle_rename(params, msg_id)
        elif method == "textDocument/documentSymbol":
            self.handle_document_symbol(params, msg_id)
        elif method == "textDocument/completion":
            self.handle_completion(params, msg_id)
        elif method == "textDocument/inlayHint":
            self.handle_inlay_hint(params, msg_id)
        else:
            # Unknown method — respond with method-not-found.
            if msg_id is not None:
                self.send_response(msg_id, None, error_code=-32601,
                                   error_message="method not found: %s" % method)

    # ---------- document sync ----------
    def handle_did_open(self, params):
        td = params.get("textDocument", {})
        uri = td.get("uri")
        text = td.get("text", "")
        version = td.get("version", 0)
        self._store_doc(uri, version, text)
        self._publish_diagnostics(uri)

    def handle_did_change(self, params):
        td = params.get("textDocument", {})
        uri = td.get("uri")
        version = td.get("version", 0)
        # Full document sync (textDocumentSync = 1): the changes array
        # contains a single change with the full text.
        changes = params.get("contentChanges", [])
        if changes:
            text = changes[0].get("text", "")
            self._store_doc(uri, version, text)
            self._publish_diagnostics(uri)

    def handle_did_close(self, params):
        td = params.get("textDocument", {})
        uri = td.get("uri")
        self.docs.pop(uri, None)
        # Stage 14 release: invalidate the cross-file index.
        self._invalidate_indexes()
        # BUG-DS4-18: per LSP spec, closing a document must CLEAR its
        # diagnostics — otherwise stale errors stay displayed forever.
        # Publish an empty diagnostics list for the closed URI.
        self.send_notification("textDocument/publishDiagnostics", {
            "uri": uri, "diagnostics": []})

    def _store_doc(self, uri, version, text):
        # SCAN-B fix: check version monotonicity — out-of-order
        # didChange notifications (race / network reorder) used to
        # overwrite newer text with older, silently dropping edits.
        if uri in self.docs and version is not None:
            cur = self.docs[uri].get("version")
            if cur is not None and version <= cur:
                return  # stale update — ignore
        # Parse + check the document. On syntax/check errors, store the
        # program as None so hover/completion degrade gracefully.
        program = None
        try:
            toks = tokenize(text.encode("utf-8"))
            program = Parser(toks).parse_program()
            try:
                check(program)
            except HLError:
                pass  # Keep the program; checker errors go to diagnostics.
        except HLError as ex:
            # Store the error for diagnostics. BUG (deep-scan-5): only the
            # message was kept, so every syntax error was anchored at
            # 0:0 even when the lexer reported a real line/col.
            program = {"_error": str(ex),
                       "_error_line": getattr(ex, "line", 0),
                       "_error_col": getattr(ex, "col", 0)}
        except (MemoryError, RecursionError, OSError):
            # SCAN-B fix: a huge file or pathological input may crash
            # tokenize/parse with a non-HLError. Store None so the
            # editor still gets a (clear) empty-diagnostics notification.
            program = None
        self.docs[uri] = {"version": version, "text": text, "program": program}
        # Stage 113: an OPEN buffer supersedes any external copy of the
        # same file — take it off the eviction list so didOpen + didClose
        # cycles cannot evict user state.
        if uri in self._ext_lru:
            self._ext_lru.remove(uri)
        # Stage 14 release: invalidate the cross-file symbol + import
        # indexes so the next definition/rename call rebuilds them with
        # the new contents.
        self._invalidate_indexes()

    def _publish_diagnostics(self, uri):
        doc = self.docs.get(uri)
        if not doc:
            return
        diagnostics = []
        prog = doc.get("program")
        if prog is None:
            # SCAN-B fix: even when program is None (tokenize/parse
            # crashed), publish an EMPTY diagnostics list so the editor
            # clears any stale markers it had from a previous version.
            self.send_notification("textDocument/publishDiagnostics", {
                "uri": uri,
                "diagnostics": [],
            })
            return
        if "_error" in prog:
            # Syntax error. Use the lexer-reported position when present
            # (BUG deep-scan-5: previously always 0:0).
            el = prog.get("_error_line", 0)
            ec = prog.get("_error_col", 0)
            eline = el - 1 if el > 0 else 0
            echar = self._byte_col_to_utf16(doc["text"], eline,
                                            ec - 1 if ec > 0 else 0)
            diagnostics.append({
                "range": {"start": {"line": eline, "character": echar},
                          "end": {"line": eline, "character": echar + 1}},
                "severity": 1,
                "source": "hls-checker",
                "message": prog["_error"],
            })
            self.send_notification("textDocument/publishDiagnostics", {
                "uri": uri, "diagnostics": diagnostics})
            return
        # Run the checker; capture any error.
        try:
            check(prog)
        except HLError as ex:
            line = ex.line - 1 if ex.line > 0 else 0
            col = ex.col - 1 if ex.col > 0 else 0
            # Deep-scan-20: lexer columns are BYTES; LSP `character` is
            # UTF-16 units — convert (see _byte_col_to_utf16).
            c16 = self._byte_col_to_utf16(doc["text"], line, col)
            diagnostics.append({
                "range": {"start": {"line": line, "character": c16},
                          "end": {"line": line, "character": c16 + 1}},
                "severity": 1,
                "source": "hls-checker",
                "message": ex.msg,
            })
        self.send_notification("textDocument/publishDiagnostics", {
            "uri": uri, "diagnostics": diagnostics})

    # ---------- hover ----------
    @staticmethod
    def _utf16_col_to_byte(text, line0, col16):
        """Convert an LSP UTF-16 `character` offset on a 0-indexed line to
        a 0-based BYTE offset in that line (the lexer's columns are
        byte-based). BUG-DS4-20: positions were previously compared as if
        UTF-16 units were bytes. BUG (deep-scan-5): the previous fix
        returned the CODE-POINT index, still desynchronising by one byte
        per non-ASCII code point (the lexer counts BYTES). Accumulate
        UTF-8 byte lengths instead."""
        lines = text.split("\n")
        if line0 < 0 or line0 >= len(lines):
            return col16
        line_bytes = lines[line0].encode("utf-8")
        units = 0
        bi = 0
        n = len(line_bytes)
        while bi < n:
            if units >= col16:
                return bi
            b = line_bytes[bi]
            if b < 0x80:
                width = 1
            elif b < 0xE0:
                width = 2
                units += 1
                bi += width
                continue
            elif b < 0xF0:
                width = 3
                units += 1
                bi += width
                continue
            else:
                width = 4
                units += 2
                bi += width
                continue
            units += 1
            bi += 1
        return n

    @staticmethod
    def _byte_col_to_utf16(text, line0, col_byte):
        """Convert a 0-based BYTE offset in a 0-indexed line to the LSP
        UTF-16 `character` offset.

        Deep-scan-20 fix (HIGH): outgoing positions (diagnostics,
        references, rename edits) were emitted with raw lexer BYTE
        columns as UTF-16 `character` values — on any line containing
        non-ASCII text (a string literal, a comment) every following
        position was shifted, and rename applied edits one character
        too far right, silently corrupting the file."""
        lines = text.split("\n")
        if line0 < 0 or line0 >= len(lines):
            return col_byte
        line_bytes = lines[line0].encode("utf-8")
        n = len(line_bytes)
        end = col_byte if 0 <= col_byte <= n else n
        units = 0
        bi = 0
        while bi < end:
            b = line_bytes[bi]
            if b < 0x80:
                width = 1
            elif b < 0xE0:
                width = 2
                units += 1
                bi += width
                continue
            elif b < 0xF0:
                width = 3
                units += 1
                bi += width
                continue
            else:
                width = 4
                units += 2
                bi += width
                continue
            units += 1
            bi += 1
        return units

    def _doc_program(self, doc):
        """Return the parsed program of a doc, or None if the doc is
        missing, unparsed, or carries a syntax error (BUG-DS4-19: the
        `_error` marker dicts used to flow into handlers that then
        crashed with KeyError: 'fns')."""
        if not doc:
            return None
        prog = doc.get("program")
        if prog is None or isinstance(prog, dict) and "_error" in prog:
            return None
        return prog

    def handle_hover(self, params, msg_id):
        td = params.get("textDocument", {})
        uri = td.get("uri")
        pos = params.get("position", {})
        line = pos.get("line", 0) + 1  # LSP is 0-indexed
        doc = self.docs.get(uri)
        if doc is None:
            self.send_response(msg_id, None)
            return
        # LSP `character` is UTF-16 units; the lexer's columns are bytes.
        col = self._utf16_col_to_byte(doc["text"], line - 1,
                                      pos.get("character", 0)) + 1
        prog = self._doc_program(doc)
        if prog is None:
            self.send_response(msg_id, None)
            return
        # Find the identifier at the given position by walking the AST.
        # Each token has line/col info; we look for an `ident` or `kw`
        # token at the given position.
        ident_name = self._ident_at(prog, line, col, uri=uri)
        if ident_name is None:
            self.send_response(msg_id, None)
            return
        # Build the hover text: identifier name + (if known) its type.
        hover_text = "**%s**" % ident_name
        # Deep-scan-20 fix: resolve the identifier in the scope of the
        # ENCLOSING function — same-named params of different functions
        # (fn alpha(n: str) + fn beta(n: int)) used to resolve by dict
        # order, showing the WRONG type on hover. Scan the tokens up to
        # the hovered position and remember the nearest preceding `fn`
        # declaration (best-effort: accurate for straight-line layouts).
        current_fn = None
        try:
            _toks = tokenize(doc["text"].encode("utf-8"))
            _expect_name = False
            for t in _toks:
                if t["k"] == "eof":
                    break
                if (t.get("line", 0), t.get("col", 0)) > (line, col):
                    break
                if t["k"] == "kw" and t["v"] == "fn":
                    _expect_name = True
                    continue
                if _expect_name and t["k"] == "ident":
                    current_fn = t["v"]
                    _expect_name = False
        except HLError:
            current_fn = None
        if current_fn is not None and current_fn not in prog.get("fns", {}):
            # impl methods are registered as "Struct.method" — try the
            # short-name match against registered keys.
            for fkey in prog.get("fns", {}):
                if fkey.split(".")[-1] == current_fn:
                    current_fn = fkey
                    break
        type_info = self._lookup_type(prog, ident_name, current_fn=current_fn)
        if type_info:
            hover_text += "\n\n```\n%s: %s\n```" % (ident_name, type_info)
        self.send_response(msg_id, {
            "contents": {"kind": "markdown", "value": hover_text}
        })

    def _ident_at(self, prog, line, col, uri=None):
        """Re-tokenise the source and return the identifier at the given
        position (ident/kw token covering 1-indexed byte line/col), or
        None.

        Stage 113: the token hunt itself moved to _token_at, which
        returns the full token so the definition handler can also see
        string literals (import paths). This wrapper keeps the old
        ident-only contract for hover/references/rename.
        """
        t = self._token_at(uri, line, col)
        if t is not None and t["k"] in ("ident", "kw"):
            return t["v"]
        return None

    def _lookup_type(self, prog, name, current_fn=None):
        """Look up the type of an identifier (param/local/field).

        Deep-scan-7 fix: the original returned the FIRST match across
        all fns/structs — two structs sharing a field name returned
        wrong type on hover. We now prefer:
          1. params of the current function (if given)
          2. function return type if name is the current fn
          3. local function declaration (name in prog["fns"])
          4. struct fields — but only return a field type if EXACTLY
             one struct has that field (ambiguous otherwise, return
             None to avoid wrong-type hover)
          5. last-resort: param of any function with matching name
             (best-effort for the legacy single-file mode)
        """
        # 1. Current function's params (highest priority).
        if current_fn is not None:
            fn = prog["fns"].get(current_fn)
            if fn:
                for (pname, ptype, _) in fn.get("params", []):
                    if pname == name:
                        return ptype
        # 2. Function declarations.
        if name in prog["fns"]:
            fn = prog["fns"][name]
            return "%s(%s) -> %s" % (
                name,
                ", ".join("%s: %s" % (p[0], p[1]) for p in fn.get("params", [])),
                fn.get("ret", "void"))
        # 3. Struct fields — only if unambiguous.
        matches = []
        for sname, sdef in prog["structs"].items():
            for (fname, ftype, _) in sdef.get("fields", []):
                if fname == name:
                    matches.append((sname, ftype))
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            # Ambiguous — return a hint listing the candidates.
            return " | ".join("%s.%s: %s" % (sn, name, ft) for sn, ft in matches)
        # 4. Last-resort: scan params of every function.
        for fname, fn in prog["fns"].items():
            for (pname, ptype, _) in fn.get("params", []):
                if pname == name:
                    return ptype
        # 5. Struct / enum type names themselves.
        if name in prog["structs"]:
            return "struct %s" % name
        if name in prog["enums"]:
            return "enum %s" % name
        return None

    # ---------- definition ----------
    def handle_definition(self, params, msg_id):
        """Stage 113: go-to-definition across packages.

        Resolution order — first hit wins:
          1. the cursor sits on an `import "..."` path literal → the
             module file itself, on disk;
          2. the current document;
          3. the files the current document imports (open buffers
             first, on-disk files parsed on demand);
          4. the workspace's package dependency sources: hls-pkg.lock
             resolved_path entries, .hls-pkg-deps/ farms, HLS_PKG_DEPS;
          5. every other open document (the Stage 14 behaviour, kept as
             the last-resort net).
        """
        td = params.get("textDocument", {})
        uri = td.get("uri")
        pos = params.get("position", {})
        line = pos.get("line", 0) + 1
        doc = self.docs.get(uri)
        if doc is None:
            self.send_response(msg_id, None)
            return
        col = self._utf16_col_to_byte(doc["text"], line - 1,
                                      pos.get("character", 0)) + 1
        prog = self._doc_program(doc)
        if prog is None:
            self.send_response(msg_id, None)
            return
        tok = self._token_at(uri, line, col)
        # 1. an import path literal IS jumpable — its definition is the
        # module file it names (std/, core/, relative, or a dependency).
        if tok is not None and tok.get("k") == "str":
            imp = self._import_token_path(prog, line, tok)
            if imp is not None:
                self.send_response(
                    msg_id, self._location_for_import(imp, uri))
                return
        if tok is None or tok.get("k") not in ("ident", "kw"):
            self.send_response(msg_id, None)
            return
        ident_name = tok["v"]
        # 2. the current document.
        loc = self._find_definition_in(uri, ident_name)
        # 3. the files this document imports.
        if loc is None:
            for iuri in self._import_uris(uri):
                if iuri == uri:
                    continue
                loc = self._find_definition_in(iuri, ident_name)
                if loc is not None:
                    break
        # 4. package dependency sources (lockfile, deps farm, env).
        if loc is None:
            for fpath in self._dep_file_list(uri):
                furi = self._path_to_uri(os.path.realpath(fpath))
                self._external_doc(fpath)
                loc = self._find_definition_in(furi, ident_name)
                if loc is not None:
                    break
        # 5. any other open document (Stage 14 last-resort net).
        if loc is None:
            for other_uri in list(self.docs):
                if other_uri == uri:
                    continue
                loc = self._find_definition_in(other_uri, ident_name)
                if loc is not None:
                    break
        self.send_response(msg_id, loc)

    def _find_definition_in(self, uri, ident_name):
        """Return a single LSP Location dict for `ident_name` in `uri`, or None."""
        doc = self.docs.get(uri)
        if doc is None:
            return None
        prog = self._doc_program(doc)
        if prog is None:
            return None
        # 1. Function definition.
        if ident_name in prog["fns"]:
            fn = prog["fns"][ident_name]
            return {
                "uri": uri,
                "range": {"start": {"line": fn.get("line", 1) - 1, "character": 0},
                          "end": {"line": fn.get("line", 1) - 1, "character": 1}},
            }
        # 2. Struct definition.
        if ident_name in prog["structs"]:
            st = prog["structs"][ident_name]
            return {
                "uri": uri,
                "range": {"start": {"line": st.get("line", 1) - 1, "character": 0},
                          "end": {"line": st.get("line", 1) - 1, "character": 1}},
            }
        # 3. Enum definition.
        if ident_name in prog["enums"]:
            en = prog["enums"][ident_name]
            return {
                "uri": uri,
                "range": {"start": {"line": en.get("line", 1) - 1, "character": 0},
                          "end": {"line": en.get("line", 1) - 1, "character": 1}},
            }
        # 4. Method definition (Struct.method).
        for fname, fn in prog["fns"].items():
            if "." in fname:
                _sname, mname = fname.split(".", 1)
                if mname == ident_name:
                    return {
                        "uri": uri,
                        "range": {"start": {"line": fn.get("line", 1) - 1, "character": 0},
                                  "end": {"line": fn.get("line", 1) - 1, "character": 1}},
                    }
        # 5. Enum variant.
        for ename, edef in prog["enums"].items():
            for variant in edef.get("variants", []):
                vname = variant[0] if isinstance(variant, (list, tuple)) else variant
                if vname == ident_name:
                    return {
                        "uri": uri,
                        "range": {"start": {"line": edef.get("line", 1) - 1, "character": 0},
                                  "end": {"line": edef.get("line", 1) - 1, "character": 1}},
                    }
        return None

    # ---------- references ----------
    def handle_references(self, params, msg_id):
        """Stage 14 release: find all references to the symbol at a position.

        Stage 113: the search spans the package layers too — the files
        the document imports and the workspace's dependency sources are
        loaded (externally, read-only) so a dependency's usages show up
        alongside the open buffers. Edit-producing callers (rename) stay
        confined to open documents; references only read.
        """
        td = params.get("textDocument", {})
        uri = td.get("uri")
        pos = params.get("position", {})
        line = pos.get("line", 0) + 1
        doc = self.docs.get(uri)
        if doc is None:
            self.send_response(msg_id, [])
            return
        col = self._utf16_col_to_byte(doc["text"], line - 1,
                                      pos.get("character", 0)) + 1
        prog = self._doc_program(doc)
        if prog is None:
            self.send_response(msg_id, [])
            return
        ident_name = self._ident_at(prog, line, col, uri=uri)
        if ident_name is None:
            self.send_response(msg_id, [])
            return
        # Search every open document for occurrences of ident_name...
        results = []
        seen = set()
        for u in list(self.docs):
            seen.add(u)
            for loc in self._find_references_in(u, ident_name):
                results.append(loc)
        # ...then the package layers: imports first, then dependency
        # sources (both parsed on demand as external documents).
        extra = []
        for iuri in self._import_uris(uri):
            if iuri not in seen:
                seen.add(iuri)
                extra.append(iuri)
        for fpath in self._dep_file_list(uri):
            furi = self._path_to_uri(os.path.realpath(fpath))
            if furi not in seen:
                seen.add(furi)
                self._external_doc(fpath)
                extra.append(furi)
        for u in extra:
            for loc in self._find_references_in(u, ident_name):
                results.append(loc)
        self.send_response(msg_id, results)

    def _find_references_in(self, uri, ident_name):
        """Yield LSP Location dicts for every textual occurrence of
        `ident_name` in `uri`. We re-tokenise the document and report
        every ident/kw token whose value matches.

        (A future, more precise implementation would track scopes so a
        local `let foo` in one function doesn't match `foo` in another.)
        """
        doc = self.docs.get(uri)
        if doc is None:
            return []
        try:
            toks = tokenize(doc["text"].encode("utf-8"))
        except HLError:
            return []
        out = []
        for t in toks:
            if t["k"] == "eof":
                break
            if t["k"] == "ident" and t["v"] == ident_name:
                ln = t.get("line", 1) - 1
                col = t.get("col", 1) - 1
                # Reconstruct token length so the highlight covers the word.
                tlen = len(t["v"]) if isinstance(t["v"], str) else len(t["v"])
                # Deep-scan-20: lexer columns are BYTES; convert the
                # start to UTF-16 units for the client. The identifier
                # itself is ASCII, so its length in UTF-16 units equals
                # its byte length.
                c16 = self._byte_col_to_utf16(doc["text"], ln, col)
                out.append({
                    "uri": uri,
                    "range": {"start": {"line": ln, "character": c16},
                              "end": {"line": ln, "character": c16 + tlen}},
                })
        return out

    # ---------- rename ----------
    def handle_rename(self, params, msg_id):
        """Stage 14 release: rename a symbol across all open documents.

        Uses _find_references_in to locate every textual occurrence of
        the identifier at `position`, then produces a WorkspaceEdit
        with TextEdits for each open document.
        """
        td = params.get("textDocument", {})
        uri = td.get("uri")
        pos = params.get("position", {})
        new_name = params.get("newName", "")
        # Validate the new name (must be a legal HLS identifier).
        if not new_name or not new_name[0].isalpha() and new_name[0] != "_":
            self.send_response(msg_id, None, error_code=-32602,
                               error_message="invalid newName: must start with a letter or _")
            return
        for c in new_name:
            if not (c.isalnum() or c == "_"):
                self.send_response(msg_id, None, error_code=-32602,
                                   error_message="invalid newName: only [A-Za-z0-9_] allowed")
                return
        doc = self.docs.get(uri)
        if doc is None:
            self.send_response(msg_id, {"changes": {}})
            return
        line = pos.get("line", 0) + 1
        col = self._utf16_col_to_byte(doc["text"], line - 1,
                                      pos.get("character", 0)) + 1
        prog = self._doc_program(doc)
        if prog is None:
            self.send_response(msg_id, {"changes": {}})
            return
        ident_name = self._ident_at(prog, line, col, uri=uri)
        if ident_name is None:
            self.send_response(msg_id, {"changes": {}})
            return
        # Don't rename keywords or builtins.
        if ident_name in KEYWORDS or ident_name in BUILTINS or ident_name in EFFECTS:
            self.send_response(msg_id, None, error_code=-32602,
                               error_message="cannot rename keyword/builtin/effect: %s" % ident_name)
            return
        # Collect edits across every open document. Stage 113: external
        # (on-disk, unopened) documents are excluded — the server will
        # not rewrite a file the user has not opened; the editor cannot
        # have unsaved buffer state for it, and a stale disk copy could
        # silently corrupt a dependency.
        changes = {}
        for u, d in self.docs.items():
            if d.get("external"):
                continue
            edits = []
            for loc in self._find_references_in(u, ident_name):
                rng = loc["range"]
                edits.append({"range": rng, "newText": new_name})
            if edits:
                changes[u] = edits
        self.send_response(msg_id, {"changes": changes})

    # ---------- document symbols ----------
    def handle_document_symbol(self, params, msg_id):
        """Stage 14 release: return the list of top-level symbols in the file.

        Powers VS Code's outline view and breadcrumb navigation.
        """
        td = params.get("textDocument", {})
        uri = td.get("uri")
        doc = self.docs.get(uri)
        if doc is None:
            self.send_response(msg_id, [])
            return
        prog = self._doc_program(doc)
        if prog is None:
            self.send_response(msg_id, [])
            return
        symbols = []
        # SymbolKind values: 12 = Function, 23 = Struct, 10 = Enum,
        # 8 = Interface (for impl), 13 = Constant.
        for fname, fn in prog["fns"].items():
            line = fn.get("line", 1) - 1
            # Determine display name: short name for methods.
            display = fname.split(".")[-1] if "." in fname else fname
            params_str = ", ".join("%s: %s" % (p[0], p[1]) for p in fn.get("params", []))
            symbols.append({
                "name": "%s(%s) -> %s" % (display, params_str, fn.get("ret", "void")),
                "kind": 12,
                "range": {"start": {"line": line, "character": 0},
                          "end": {"line": line, "character": 1}},
                "selectionRange": {"start": {"line": line, "character": 0},
                                    "end": {"line": line, "character": len(display)}},
            })
        for sname, sdef in prog["structs"].items():
            line = sdef.get("line", 1) - 1
            symbols.append({
                "name": "struct %s" % sname,
                "kind": 23,
                "range": {"start": {"line": line, "character": 0},
                          "end": {"line": line, "character": 1}},
                "selectionRange": {"start": {"line": line, "character": 0},
                                    "end": {"line": line, "character": len(sname)}},
            })
        for ename, edef in prog["enums"].items():
            line = edef.get("line", 1) - 1
            symbols.append({
                "name": "enum %s" % ename,
                "kind": 10,
                "range": {"start": {"line": line, "character": 0},
                          "end": {"line": line, "character": 1}},
                "selectionRange": {"start": {"line": line, "character": 0},
                                    "end": {"line": line, "character": len(ename)}},
            })
        # Sort by line for a stable outline.
        symbols.sort(key=lambda s: s["range"]["start"]["line"])
        self.send_response(msg_id, symbols)

    # ---------- completion ----------
    def handle_completion(self, params, msg_id):
        items = []
        seen = set()
        # Deep-scan fix (D6): use proper LSP CompletionItemKind values.
        # 14 = Keyword, 3 = Function, 13 = Enum (for effects).
        # Previously every item was tagged Keyword, which deprived
        # editors of semantic categorisation.
        for kw in KEYWORDS:
            items.append({"label": kw, "kind": 14})  # Keyword
            seen.add(kw)
        for b in BUILTINS:
            items.append({"label": b, "kind": 3})    # Function
            seen.add(b)
        for e in EFFECTS:
            items.append({"label": e, "kind": 13})   # Enum
            seen.add(e)
        # Add identifiers from the program matching the requested URI.
        td = params.get("textDocument", {})
        uri = td.get("uri")
        doc = self.docs.get(uri) if uri else None
        if doc is None:
            # Fall back to the first available document.
            for u, d in self.docs.items():
                doc = d
                break
        prog = self._doc_program(doc)
        if prog is not None:
            for fname in prog["fns"]:
                if fname not in seen:
                    items.append({"label": fname, "kind": 3})  # 3 = Function
                    seen.add(fname)
            for sname in prog["structs"]:
                if sname not in seen:
                    items.append({"label": sname, "kind": 7})  # 7 = Class
                    seen.add(sname)
            for ename in prog["enums"]:
                if ename not in seen:
                    items.append({"label": ename, "kind": 13})  # 13 = Enum
                    seen.add(ename)
        # Stage 113: symbols from the files this document imports — open
        # or parsed on demand — so a dependency's API completes the moment
        # its import is in the file. Dependency sources beyond the direct
        # imports stay lazy: they join the offering once any lookup has
        # loaded them (external docs are in self.docs and indexed).
        if uri is not None:
            for iuri in self._import_uris(uri):
                iprog = self._doc_program(self.docs.get(iuri))
                if iprog is None:
                    continue
                for fname in iprog["fns"]:
                    if fname not in seen:
                        items.append({"label": fname, "kind": 3})
                        seen.add(fname)
                for sname in iprog["structs"]:
                    if sname not in seen:
                        items.append({"label": sname, "kind": 7})
                        seen.add(sname)
                for ename in iprog["enums"]:
                    if ename not in seen:
                        items.append({"label": ename, "kind": 13})
                        seen.add(ename)
        self.send_response(msg_id, items)

    # ---------- inlay hints (Stage 114) ----------

    def handle_inlay_hint(self, params, msg_id):
        """Stage 114: textDocument/inlayHint — parameter names at call
        sites, types on match-arm payload bindings.

        The AST records no columns, so hint POSITIONS come from the
        token stream: the AST is walked in source order (children in
        field order, statements in block order, top-level items sorted
        by their line) while a monotonic token cursor re-locates each
        call site / match arm. A site that cannot be re-located is
        skipped, never guessed at. Resolution mirrors the checker:
        builtins first, then local fns, then the files the document
        imports; a hint is emitted only when the argument count
        exactly matches the parameter count. Failures degrade to
        fewer hints, never to a wrong one, and never to an error —
        an inlay hint must not be able to take the server down.
        """
        td = params.get("textDocument", {})
        uri = td.get("uri")
        doc = self.docs.get(uri)
        prog = self._doc_program(doc) if doc else None
        if prog is None:
            self.send_response(msg_id, [])
            return
        rng = params.get("range") or {}
        lo = (rng.get("start") or {}).get("line", 0)
        hi = (rng.get("end") or {}).get("line", 1 << 30)
        text = doc["text"]
        try:
            toks = tokenize(text.encode("utf-8"))
        except HLError:
            self.send_response(msg_id, [])
            return

        # The in-order site walk (below) feeds two monotonic token
        # cursors — one per hint family, each family scanned in source
        # order so neither can desynchronise the other.
        sites = []
        self._walk_program(prog, sites)

        hints = []
        call_i = 0   # cursor for call/fieldcall site relocation
        match_i = 0  # cursor for match-arm arrow relocation
        for kind, node in sites:
            if kind == "match":
                match_i = self._match_binding_hints(prog, toks, text, node,
                                                    match_i, lo, hi, hints)
            else:
                call_i = self._call_param_hints(uri, prog, toks, text,
                                                node, kind == "fieldcall",
                                                call_i, lo, hi, hints)
        hints.sort(key=lambda h: (h["position"]["line"],
                                  h["position"]["character"]))
        self.send_response(msg_id, hints)

    # -- the in-order walk ------------------------------------------------

    def _walk_program(self, prog, out):
        """Yield ("call"|"fieldcall"|"match", node) for every call site
        and match expression in `prog`, in SOURCE order.

        Top-level items are sorted by line (fns carry their keyword
        line, structs theirs); struct field defaults are walked too —
        they are expressions and can call. Enum declarations hold no
        expressions. The order property is what lets the token cursor
        stay monotonic: a node's tokens are never revisited after the
        walk has moved past them."""
        items = []
        for fn in prog.get("fns", {}).values():
            items.append((fn.get("line", 0), fn.get("body") or [], True))
        for sdef in prog.get("structs", {}).values():
            dflts = [f[2] for f in sdef.get("fields", [])
                     if isinstance(f[2], dict)]
            if dflts:
                items.append((sdef.get("line", 0), dflts, False))
        items.sort(key=lambda it: it[0])
        for _line, payload, is_stmts in items:
            if is_stmts:
                for st in payload:
                    self._walk_stmt(st, out)
            else:
                for e in payload:
                    self._walk_expr(e, out)

    def _walk_stmt(self, s, out):
        if not isinstance(s, dict):
            return
        k = s.get("k")
        if k == "let":
            self._walk_expr(s.get("value"), out)
        elif k == "assign":
            self._walk_expr(s.get("target"), out)
            self._walk_expr(s.get("value"), out)
        elif k == "return":
            self._walk_expr(s.get("value"), out)
        elif k == "expr":
            self._walk_expr(s.get("e"), out)
        elif k == "if":
            self._walk_expr(s.get("cond"), out)
            for st in s.get("then") or []:
                self._walk_stmt(st, out)
            for st in s.get("els") or []:
                self._walk_stmt(st, out)
        elif k == "while":
            self._walk_expr(s.get("cond"), out)
            for st in s.get("body") or []:
                self._walk_stmt(st, out)
        elif k == "for":
            self._walk_expr(s.get("iter"), out)
            for st in s.get("body") or []:
                self._walk_stmt(st, out)
        elif k == "asm":
            # asm! operands are (constraint, expr) pairs; the
            # constraint side is a plain string and walks as a no-op.
            for op in s.get("operands") or []:
                if isinstance(op, (list, tuple)):
                    for part in op:
                        self._walk_expr(part, out)
                else:
                    self._walk_expr(op, out)

    def _walk_expr(self, e, out):
        if not isinstance(e, dict):
            return
        k = e.get("k")
        if k == "call":
            # The callee name token precedes its arguments: yield the
            # node first, then recurse.
            out.append(("call", e))
            for a in e.get("args") or []:
                self._walk_expr(a, out)
        elif k == "fieldcall" or k == "method":
            # `target.name(args)` — the checker REWRITES a resolved
            # method call in place (fieldcall -> method) while an
            # enum-variant constructor keeps fieldcall, and a node the
            # checker never reached keeps its parsed kind. Both shapes
            # carry the same fields and the same source order: the
            # target's tokens precede the `.name(`, so recurse into
            # the target first, then yield, then the arguments.
            self._walk_expr(e.get("target"), out)
            out.append(("fieldcall", e))
            for a in e.get("args") or []:
                self._walk_expr(a, out)
        elif k == "field":
            self._walk_expr(e.get("target"), out)
        elif k == "index":
            self._walk_expr(e.get("target"), out)
            self._walk_expr(e.get("idx"), out)
        elif k == "bin":
            self._walk_expr(e.get("l"), out)
            self._walk_expr(e.get("r"), out)
        elif k == "un":
            self._walk_expr(e.get("e"), out)
        elif k == "qmark":
            self._walk_expr(e.get("e"), out)
        elif k == "listlit":
            for item in e.get("items") or []:
                self._walk_expr(item, out)
        elif k == "structlit":
            for _fname, fv in e.get("fields") or []:
                self._walk_expr(fv, out)
        elif k == "match":
            out.append(("match", e))
            self._walk_expr(e.get("scrut"), out)
            for arm in e.get("arms") or []:
                self._walk_expr(arm.get("body"), out)

    # -- parameter-name hints ---------------------------------------------

    def _call_param_hints(self, uri, prog, toks, text, node, is_method,
                          cursor, lo_line, hi_line, hints):
        """Locate one call site in the token stream from `cursor`,
        resolve its parameter names, and append one hint per argument.
        Returns the advanced cursor. Every guard degrades to "no
        hints for this site": unresolved callee, arity mismatch,
        unlocated site, enum-variant constructor."""
        name = node.get("name")
        node_line = node.get("line", 0)
        if not isinstance(name, str) or not name:
            return cursor
        paren = self._scan_call_site(toks, cursor, name, node_line,
                                     is_method)
        if paren is None:
            # Not found — leave the cursor alone; a later site's scan
            # starting earlier can only match its own shape.
            return cursor
        if is_method:
            params = self._resolve_method_params(uri, prog, toks, paren,
                                                 name)
        else:
            params = self._resolve_fn_params(uri, prog, name)
        if not params:
            return paren + 1
        spans = self._arg_spans(toks, paren)
        if spans is None or len(spans) != len(params):
            return paren + 1
        callee_label = name if not is_method else "." + name
        for i, (pname, _ptype) in enumerate(params):
            start, end = spans[i]
            t = toks[start]
            # Redundancy rule: `f(x: x)` — a single bare identifier
            # argument spelled exactly like its parameter — says
            # nothing the source does not already say.
            if end - start == 1 and t["k"] == "ident" and t["v"] == pname:
                continue
            line0 = t["line"] - 1
            if line0 < lo_line or line0 > hi_line:
                continue
            char16 = self._byte_col_to_utf16(text, line0, t["col"] - 1)
            hints.append({
                "position": {"line": line0, "character": char16},
                "label": pname + ":",
                "kind": 2,  # InlayHintKind.Parameter
                "paddingLeft": False,
                "paddingRight": True,
                "tooltip": "parameter '%s' of '%s'" % (pname, callee_label),
            })
        return paren + 1

    @staticmethod
    def _scan_call_site(toks, start, name, node_line, want_method):
        """Find the open-paren token index of the call site for `name`
        at/after `start`, preferring a match on the node's own line
        (the AST line is the callee-name token's line); a match on a
        LATER line is the multi-line spelling fallback; a match on an
        EARLIER line is stale — skipped. Returns the paren index or
        None."""
        n = len(toks)
        fallback = None
        i = start
        while i < n:
            t = toks[i]
            if want_method:
                if (t["k"] == "sym" and t["v"] == "." and i + 2 < n
                        and toks[i + 1]["k"] == "ident"
                        and toks[i + 1]["v"] == name
                        and toks[i + 2]["k"] == "sym"
                        and toks[i + 2]["v"] == "("):
                    ln = toks[i + 1]["line"]
                    if ln == node_line:
                        return i + 2
                    if fallback is None and ln > node_line:
                        fallback = i + 2
                    i += 3
                    continue
            else:
                if (t["k"] == "ident" and t["v"] == name and i + 1 < n
                        and toks[i + 1]["k"] == "sym"
                        and toks[i + 1]["v"] == "("):
                    ln = t["line"]
                    if ln == node_line:
                        return i + 1
                    if fallback is None and ln > node_line:
                        fallback = i + 1
                    i += 2
                    continue
            i += 1
        return fallback

    @staticmethod
    def _arg_spans(toks, paren):
        """Top-level argument token spans [(start, end_exclusive), ...]
        of the call whose open paren sits at `paren`, or None when the
        paren group never closes. Nesting counts brackets of every
        shape; strings are single tokens and cannot confuse the
        depth counter."""
        n = len(toks)
        spans = []
        depth = 1
        j = paren + 1
        expect_arg = True
        start = None
        while j < n:
            t = toks[j]
            if depth == 1 and expect_arg:
                start = j
                expect_arg = False
            if t["k"] == "sym":
                v = t["v"]
                if v in ("(", "[", "{"):
                    depth += 1
                elif v in (")", "]", "}"):
                    depth -= 1
                    if depth == 0:
                        if start is not None:
                            spans.append((start, j))
                        return spans
                elif v == "," and depth == 1:
                    if start is not None:
                        spans.append((start, j))
                    expect_arg = True
            j += 1
        return None

    def _resolve_fn_params(self, uri, prog, name):
        """[(param_name, type), ...] for a bare call, in the checker's
        own resolution order: builtin table (a user fn cannot shadow
        one — check_call consults BUILTIN_FNS first), then the local
        program, then the files the document imports. Two imports
        defining the same name with different signatures is
        ambiguous: None. Never None-vs-missing confusion: an empty
        parameter list is a real result ([])."""
        names = BUILTIN_PARAM_NAMES.get(name)
        if names is not None:
            return [(p, None) for p in names]
        fn = prog.get("fns", {}).get(name)
        if fn is not None:
            return [(p, t) for (p, t, _m) in fn.get("params", [])]
        found = None
        try:
            iuris = self._import_uris(uri)
        except Exception:
            iuris = []
        for iuri in iuris:
            iprog = self._doc_program(self.docs.get(iuri))
            if iprog is None:
                continue
            ifn = iprog.get("fns", {}).get(name)
            if ifn is None:
                continue
            sig = [(p, t) for (p, t, _m) in ifn.get("params", [])]
            if found is None:
                found = sig
            elif found != sig:
                return None
        return found

    def _resolve_method_params(self, uri, prog, toks, paren, name):
        """[(param_name, type), ...] for a `.name(` site, receiver
        excluded (the checker strips params[0]). The receiver's type
        is not tracked here, so the rule is unique-candidate:
        exactly one user method of that name across the local program
        and the imports — and never when a builtin method of the same
        name exists (the receiver could be either). An
        `Enum.Variant(...)` constructor is recognised from the token
        before the dot and skipped: payloads have no names."""
        # Enum-variant constructor: `Color.RGB(...)` — the head token
        # names a local enum and the method name is one of its
        # variants.
        if paren >= 3:
            head = toks[paren - 3]
            if head["k"] == "ident":
                edef = prog.get("enums", {}).get(head["v"])
                if edef is not None:
                    for vname, _pls in edef.get("variants", []):
                        if vname == name:
                            return None
        # User methods — unique candidate only.
        cands = set()
        for key in prog.get("fns", {}):
            if key.endswith("." + name):
                cands.add(key)
        try:
            iuris = self._import_uris(uri)
        except Exception:
            iuris = []
        for iuri in iuris:
            iprog = self._doc_program(self.docs.get(iuri))
            if iprog is None:
                continue
            for key in iprog.get("fns", {}):
                if key.endswith("." + name):
                    cands.add(key)
        if cands:
            if len(cands) != 1 or name in BUILTIN_METHOD_PARAM_NAMES:
                return None  # ambiguous, or shadows a builtin method
            key = cands.pop()
            fn = prog.get("fns", {}).get(key)
            if fn is None:
                # The unique candidate came from an import.
                for iuri in iuris:
                    iprog = self._doc_program(self.docs.get(iuri))
                    if iprog:
                        fn = iprog.get("fns", {}).get(key)
                        if fn is not None:
                            break
            if fn is None:
                return None
            return [(p, t) for (p, t, _m) in fn.get("params", [])[1:]]
        # Builtin methods — the table already maps ambiguous names
        # (list.set vs map.set) to None.
        names = BUILTIN_METHOD_PARAM_NAMES.get(name)
        if names is None:
            return None
        return [(p, None) for p in names]

    # -- match-binding type hints -----------------------------------------

    def _match_binding_hints(self, prog, toks, text, node, cursor,
                             lo_line, hi_line, hints):
        """Append `: T` hints after each match-arm payload binding.
        Types prefer the checker's own instantiation (arm["binds"],
        present when check() reached the arm — generic enums show the
        INSTANTIATED payload type), falling back to the raw variant
        payloads of a fully-qualified pattern. The arm's arrow is
        re-located with the monotonic match cursor; an arm that
        cannot be located ends the match's hinting (later arms would
        be guesses). Returns the advanced cursor."""
        n = len(toks)
        # Advance past the `match` keyword itself.
        i = cursor
        while i < n and not (toks[i]["k"] == "kw" and toks[i]["v"] == "match"):
            i += 1
        if i >= n:
            return cursor
        i += 1
        for arm in node.get("arms") or []:
            # Find this arm's `=>` — the first after the previous
            # arm's; patterns contain no arrows.
            arrow = None
            while i < n:
                if toks[i]["k"] == "sym" and toks[i]["v"] == "=>":
                    arrow = i
                    break
                i += 1
            if arrow is None:
                return i
            pat = arm.get("pattern") or {}
            binds = arm.get("binds")
            payload_types = None
            if binds is None and pat.get("k") == "variant" and pat.get("enum"):
                edef = prog.get("enums", {}).get(pat["enum"])
                if edef is not None:
                    for vname, pls in edef.get("variants", []):
                        if vname == pat["variant"]:
                            payload_types = pls
                            break
            if pat.get("k") == "variant" and pat.get("has_paren"):
                # The binding identifiers are the top-level idents of
                # the paren group that closes right before the arrow —
                # `Name.Variant(a, b) =>`. Walking BACKWARD from the
                # arrow cannot be confused by braces in the scrutinee
                # or in an earlier arm's body (a struct literal there
                # would tilt any forward depth counter).
                idents = self._pattern_bindings_before(toks, arrow)
                if binds:
                    bmap = dict(binds)
                    for t in idents:
                        btype = bmap.get(t["v"])
                        self._binding_hint(toks, text, t, btype,
                                           lo_line, hi_line, hints)
                elif payload_types is not None:
                    if len(idents) == len(payload_types):
                        for t, btype in zip(idents, payload_types):
                            self._binding_hint(toks, text, t, btype,
                                               lo_line, hi_line, hints)
            i = arrow + 1
        return i

    @staticmethod
    def _pattern_bindings_before(toks, arrow):
        """Binding identifier tokens inside the `(...)` group that
        closes immediately before `arrow` (a `sym =>` token index),
        in source order. Returns [] when the arrow is not preceded by
        a balanced paren group — a wildcard arm, a bare variant
        pattern, or anything this backward scan refuses to guess
        about."""
        n = len(toks)
        j = arrow - 1
        if j < 0 or toks[j]["k"] != "sym" or toks[j]["v"] != ")":
            return []
        depth = 1
        idents = []
        j -= 1
        while j >= 0 and depth > 0:
            t = toks[j]
            if t["k"] == "sym":
                if t["v"] in (")", "]", "}"):
                    depth += 1
                elif t["v"] in ("(", "[", "{"):
                    depth -= 1
                    if depth == 0:
                        break
            elif t["k"] == "ident" and depth == 1 and t["v"] != "_":
                idents.append(t)
            j -= 1
        idents.reverse()
        return idents

    def _binding_hint(self, toks, text, tok, btype, lo_line, hi_line,
                      hints):
        if not isinstance(btype, str) or not btype:
            return
        line0 = tok["line"] - 1
        if line0 < lo_line or line0 > hi_line:
            return
        # Position: just past the binding identifier.
        off = tok["col"] - 1 + len(tok["v"].encode("utf-8"))
        char16 = self._byte_col_to_utf16(text, line0, off)
        hints.append({
            "position": {"line": line0, "character": char16},
            "label": ": " + btype,
            "kind": 1,  # InlayHintKind.Type
            "paddingLeft": False,
            "paddingRight": False,
            "tooltip": "inferred match binding type",
        })

    # ---------- helpers ----------
    def send_response(self, msg_id, result, error_code=None, error_message=None):
        msg = {"jsonrpc": "2.0", "id": msg_id}
        if error_code is not None:
            msg["error"] = {"code": error_code, "message": error_message}
        else:
            msg["result"] = result
        write_message(msg)

    def send_notification(self, method, params):
        write_message({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# One-shot check mode (for non-LSP editors).
# ---------------------------------------------------------------------------

def one_shot_check(path):
    """Print diagnostics for a file to stdout (one-shot mode)."""
    if not os.path.isfile(path):
        sys.stderr.write("error: file not found: %s\n" % path)
        return 1
    with open(path, "rb") as f:
        src = f.read()
    try:
        toks = tokenize(src)
        program = Parser(toks).parse_program()
    except HLError as ex:
        print("%s:%d:%d: error: %s" % (path, ex.line, ex.col, ex.msg))
        return 1
    try:
        check(program)
    except HLError as ex:
        print("%s:%d:%d: error: %s" % (path, ex.line, ex.col, ex.msg))
        return 1
    print("%s: OK (types and effects valid)" % path)
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="hls-lsp",
        description="Halis language server (Stage 14-alpha).")
    parser.add_argument("--check", metavar="FILE.hls",
                        help="One-shot: print diagnostics to stdout and exit.")
    args = parser.parse_args()
    if args.check:
        return one_shot_check(args.check)
    HLSServer().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
