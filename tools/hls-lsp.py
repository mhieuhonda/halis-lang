#!/usr/bin/env python3
"""hls-lsp — Language Server for Halis (HLS).

Stage 14 release (v0.24.0-alpha): cross-file go-to-definition with
import resolution, rename refactoring, document symbols, document
highlight, and signature-info. Editor plugins (VS Code, Neovim) ship
under editors/.

Stage 14-alpha (v0.12.0-alpha): minimal LSP server over JSON-RPC stdio.

Implemented methods:
  initialize / shutdown / exit
  textDocument/didOpen / didChange / didClose (full document sync)
  textDocument/hover        — show the inferred type of an identifier at
                              a position (uses the checker's annotations).
  textDocument/definition   — find the function/struct/enum definition at
                              a position. Searches imported files too
                              (cross-file go-to-definition, Stage 14
                              release target).
  textDocument/references   — find all references to the symbol at a
                              position (used by rename preflight).
  textDocument/rename      — rename the symbol at a position across
                              all open documents (Stage 14 release target).
  textDocument/documentSymbol — list every top-level fn/struct/enum in
                              the file (used by VS Code's outline view).
  textDocument/completion   — basic keyword + identifier completion.
  textDocument/publishDiagnostics (notification) — runs the Stage-0
                              checker and publishes errors as diagnostics.

Usage:
  hls-lsp                    # start the server on stdio
  hls-lsp --check FILE.hls   # one-shot: print diagnostics to stdout
                              (useful for editors that don't speak LSP)

Status: Stage 14 release. The LSP server uses the Stage-0
lexer/parser/checker internally. Editor plugins live under
editors/vscode/ and editors/neovim/.
"""
import argparse
import json
import os
import sys
import traceback

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
                },
                "serverInfo": {
                    "name": "hls-lsp",
                    "version": "0.14.0-alpha",
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
            echar = ec - 1 if ec > 0 else 0
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
            diagnostics.append({
                "range": {"start": {"line": line, "character": col},
                          "end": {"line": line, "character": col + 1}},
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
        type_info = self._lookup_type(prog, ident_name)
        if type_info:
            hover_text += "\n\n```\n%s: %s\n```" % (ident_name, type_info)
        self.send_response(msg_id, {
            "contents": {"kind": "markdown", "value": hover_text}
        })

    def _ident_at(self, prog, line, col, uri=None):
        """Re-tokenise the source and find the identifier at the given position.

        If `uri` is given, use that document; otherwise fall back to the
        first available document (best-effort for legacy callers).
        """
        doc = None
        if uri is not None and uri in self.docs:
            doc = self.docs[uri]
        else:
            for u, d in self.docs.items():
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
            # Deep-scan-12 fix (DSS-T-09): use the lexer's `raw` field
            # when present (it's set on int / float tokens). `str(v)`
            # loses information: `1_000` (raw) becomes `1000` (4 chars
            # vs the source's 5), and `0.01` formatted via `repr` may
            # produce `0.01` or scientific notation depending on the
            # value. The lexer's `raw` is the EXACT source substring
            # the highlighter must use to compute the token's extent.
            if "raw" in t and isinstance(t["raw"], (str, bytes)):
                tlen = len(t["raw"]) if isinstance(t["raw"], str) \
                    else len(t["raw"])
            elif isinstance(t["v"], bytes):
                tlen = len(t["v"])
            else:
                tlen = len(str(t["v"]))
            if t["line"] == line and t["col"] <= col < t["col"] + tlen:
                if t["k"] in ("ident", "kw"):
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
        ident_name = self._ident_at(prog, line, col, uri=uri)
        if ident_name is None:
            self.send_response(msg_id, None)
            return
        # Stage 14 release: cross-file go-to-definition.
        # First check the current document; if not found, search every
        # other open document (imported files in particular).
        loc = self._find_definition_in(uri, ident_name)
        if loc is None:
            # Try every other open document.
            for other_uri in self.docs:
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
        """Stage 14 release: find all references to the symbol at a position."""
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
        # Search every open document for occurrences of ident_name.
        results = []
        for u, d in self.docs.items():
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
                out.append({
                    "uri": uri,
                    "range": {"start": {"line": ln, "character": col},
                              "end": {"line": ln, "character": col + tlen}},
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
        # Collect edits across every open document.
        changes = {}
        for u, d in self.docs.items():
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
        self.send_response(msg_id, items)

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
