#!/usr/bin/env python3
"""hls-lsp — Language Server for Hieu Louis (HLS).

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
    """Read a single JSON-RPC message from stdin (Content-Length framing)."""
    headers = {}
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
    if b"content-length" not in headers:
        return None
    n = int(headers[b"content-length"])
    body = sys.stdin.buffer.read(n)
    return json.loads(body.decode("utf-8"))


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
            "false", "enum", "match", "pure"]
BUILTINS = ["println", "print", "len", "str", "int", "float", "bool", "panic",
            "clock_ms", "args", "exit", "chr", "ord", "range", "map_new",
            "list_new", "drop", "clone", "take", "file_exists", "read_file",
            "write_file", "tainted_args", "taint_mark", "taint_unwrap",
            "read_file_tainted"]
EFFECTS = ["IO", "Fs", "Clock", "Args", "Exit"]


class HLSServer:
    def __init__(self):
        # Map of uri -> {"version": int, "text": str, "program": dict}
        self.docs = {}
        self.shutdown_requested = False

    def run(self):
        while not self.shutdown_requested:
            msg = read_message()
            if msg is None:
                break
            try:
                self.handle(msg)
            except Exception as ex:
                sys.stderr.write("error handling message: %s\n" % ex)
                traceback.print_exc(file=sys.stderr)

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
                    "version": "0.12.0-alpha",
                },
            })
        elif method == "initialized":
            pass  # no-op
        elif method == "shutdown":
            self.shutdown_requested = True
            self.send_response(msg_id, None)
        elif method == "exit":
            sys.exit(0)
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

    def _store_doc(self, uri, version, text):
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
            # Store the error for diagnostics.
            program = {"_error": str(ex)}
        self.docs[uri] = {"version": version, "text": text, "program": program}

    def _publish_diagnostics(self, uri):
        doc = self.docs.get(uri)
        if not doc:
            return
        diagnostics = []
        prog = doc.get("program")
        if prog is None:
            return
        if "_error" in prog:
            # Syntax error.
            diagnostics.append({
                "range": {"start": {"line": 0, "character": 0},
                          "end": {"line": 0, "character": 1}},
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
    def handle_hover(self, params, msg_id):
        td = params.get("textDocument", {})
        uri = td.get("uri")
        pos = params.get("position", {})
        line = pos.get("line", 0) + 1  # LSP is 0-indexed
        col = pos.get("character", 0) + 1
        doc = self.docs.get(uri)
        if not doc or doc.get("program") is None:
            self.send_response(msg_id, None)
            return
        prog = doc["program"]
        # Find the identifier at the given position by walking the AST.
        # Each token has line/col info; we look for an `ident` or `kw`
        # token at the given position.
        ident_name = self._ident_at(prog, line, col)
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

    def _ident_at(self, prog, line, col):
        """Re-tokenise the source and find the identifier at the given position."""
        doc = None
        for uri, d in self.docs.items():
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
            if t["line"] == line and t["col"] <= col < t["col"] + len(str(t["v"])):
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
        col = pos.get("character", 0) + 1
        doc = self.docs.get(uri)
        if not doc or doc.get("program") is None:
            self.send_response(msg_id, None)
            return
        prog = doc["program"]
        ident_name = self._ident_at(prog, line, col)
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
        for kw in KEYWORDS + BUILTINS + EFFECTS:
            items.append({"label": kw, "kind": 14})  # 14 = Keyword
            seen.add(kw)
        # Add identifiers from the program.
        doc = None
        for uri, d in self.docs.items():
            doc = d
            break
        if doc and doc.get("program") is not None:
            prog = doc["program"]
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
        description="Hieu Louis language server (Stage 14-alpha).")
    parser.add_argument("--check", metavar="FILE.hls",
                        help="One-shot: print diagnostics to stdout and exit.")
    args = parser.parse_args()
    if args.check:
        return one_shot_check(args.check)
    HLSServer().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
