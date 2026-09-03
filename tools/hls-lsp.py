#!/usr/bin/env python3
"""hls-lsp — Language Server for Halis (HLS).

Stage 14 (v0.12.0-alpha): minimal LSP server over JSON-RPC stdio.

Implemented methods:
  initialize / shutdown
  textDocument/didOpen
  textDocument/didChange
  textDocument/didClose
  textDocument/hover        — show the inferred type of an identifier at
                              a position (uses the checker's annotations).
  textDocument/definition   — find the function/struct/enum definition at
                              a position (searches the AST for the matching
                              name).
  textDocument/completion   — basic keyword + identifier completion.
  textDocument/publishDiagnostics (notification) — runs the Stage-0
                              checker and publishes errors as diagnostics.

Usage:
  hls-lsp                    # start the server on stdio
  hls-lsp --check FILE.hls   # one-shot: print diagnostics to stdout
                              (useful for editors that don't speak LSP)

Status: alpha. The LSP server uses the Stage-0 lexer/parser/checker
internally. Full go-to-definition across files (with import resolution)
is the Stage 14 release target.
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
# Only actual HLS builtin functions (see boot/checker.py BUILTIN_FNS).
BUILTINS = ["println", "print", "len", "str", "int", "panic",
            "clock_ms", "args", "exit", "chr", "range", "map_new",
            "drop", "clone", "take", "file_exists", "read_file",
            "write_file", "tainted_args", "taint_mark", "taint_unwrap",
            "read_file_tainted"]
# Deep-scan fix (C5): include Net, Rand, Proc (Stage 9 release) so editor
# autocompletion offers them when the user types `uses `.
EFFECTS = ["IO", "Fs", "Clock", "Args", "Exit", "Net", "Rand", "Proc"]


class HLSServer:
    def __init__(self):
        # Map of uri -> {"version": int, "text": str, "program": dict}
        self.docs = {}
        self.shutdown_requested = False

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
            tlen = len(str(t["v"])) if not isinstance(t["v"], bytes) else len(t["v"])
            if t["line"] == line and t["col"] <= col < t["col"] + tlen:
                if t["k"] in ("ident", "kw"):
                    return t["v"]
        return None

    def _lookup_type(self, prog, name):
        """Look up the type of an identifier (param/local/field)."""
        # Look through each function's params first.
        for fname, fn in prog["fns"].items():
            for (pname, ptype, _) in fn["params"]:
                if pname == name:
                    return ptype
        # Struct fields.
        for sname, sdef in prog["structs"].items():
            for (fname, ftype, _) in sdef["fields"]:
                if fname == name:
                    return ftype
        # Function return type.
        if name in prog["fns"]:
            fn = prog["fns"][name]
            return "%s(%s) -> %s" % (
                name,
                ", ".join("%s: %s" % (p[0], p[1]) for p in fn["params"]),
                fn["ret"])
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
        # Search for the definition.
        # 1. Function definition.
        if ident_name in prog["fns"]:
            fn = prog["fns"][ident_name]
            self.send_response(msg_id, {
                "uri": uri,
                "range": {"start": {"line": fn["line"] - 1, "character": 0},
                          "end": {"line": fn["line"] - 1, "character": 1}},
            })
            return
        # 2. Struct definition.
        if ident_name in prog["structs"]:
            st = prog["structs"][ident_name]
            self.send_response(msg_id, {
                "uri": uri,
                "range": {"start": {"line": st["line"] - 1, "character": 0},
                          "end": {"line": st["line"] - 1, "character": 1}},
            })
            return
        # 3. Enum definition.
        if ident_name in prog["enums"]:
            en = prog["enums"][ident_name]
            self.send_response(msg_id, {
                "uri": uri,
                "range": {"start": {"line": en["line"] - 1, "character": 0},
                          "end": {"line": en["line"] - 1, "character": 1}},
            })
            return
        # 4. Method definition (Struct.method).
        for fname, fn in prog["fns"].items():
            if "." in fname:
                sname, mname = fname.split(".", 1)
                if mname == ident_name:
                    self.send_response(msg_id, {
                        "uri": uri,
                        "range": {"start": {"line": fn["line"] - 1, "character": 0},
                                  "end": {"line": fn["line"] - 1, "character": 1}},
                    })
                    return
        self.send_response(msg_id, None)

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
