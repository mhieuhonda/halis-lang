#!/usr/bin/env python3
"""lsp_smoke — protocol-level smoke test for tools/hls-lsp.py.

Runs the real server as a subprocess over stdio JSON-RPC and asserts the
resilience fixes that shipped in v0.16.0-alpha:

  1. BUG-DS4-16 — one malformed JSON-RPC frame must NOT kill the server;
     the spec requires a -32700 Parse error response and the loop continues.
  2. BUG-DS4-17/19 — a hover request against a document with a SYNTAX
     ERROR must return a result (null) instead of crashing with
     KeyError: 'fns' and leaving the request unanswered (which hung the
     editor until timeout).
  3. BUG-DS4-18 — didClose must publish an EMPTY diagnostics list (clear
     stale errors).
  4. shutdown → exit lifecycle must exit 0.

Exit code 0 = all assertions passed.
"""
import json
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def frame(obj):
    body = json.dumps(obj).encode()
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)


def parse_frames(buf):
    msgs = []
    while b"Content-Length:" in buf:
        i = buf.index(b"Content-Length:")
        j = buf.index(b"\r\n\r\n", i)
        n = int(buf[i + 16:j])
        msgs.append(json.loads(buf[j + 4:j + 4 + n]))
        buf = buf[j + 4 + n:]
    return msgs


def main():
    bad_body = b'{"jsonrpc"::'  # deliberately invalid JSON, correct length
    bad = b"Content-Length: %d\r\n\r\n%s" % (len(bad_body), bad_body)

    payload = (
        bad +
        frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) +
        frame({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {
                "uri": "file:///smoke.hls", "version": 1,
                "text": "fn main() -> int { let total: int = 1 +"}}}) +
        frame({"jsonrpc": "2.0", "id": 42, "method": "textDocument/hover",
               "params": {"textDocument": {"uri": "file:///smoke.hls"},
                          "position": {"line": 0, "character": 29}}}) +
        frame({"jsonrpc": "2.0", "method": "textDocument/didClose", "params": {
            "textDocument": {"uri": "file:///smoke.hls"}}}) +
        frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"}) +
        frame({"jsonrpc": "2.0", "method": "exit"})
    )

    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "hls-lsp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = proc.communicate(payload, timeout=60)

    failures = []

    # 1. Server survived the malformed frame and answered it.
    msgs = parse_frames(out)
    parse_errors = [m for m in msgs if m.get("error", {}).get("code") == -32700]
    if not parse_errors:
        failures.append("no -32700 Parse error response for the malformed frame")
    if proc.returncode != 0:
        failures.append("server exit code %r (expected 0 after exit "
                        "notification following shutdown)" % proc.returncode)

    # 2. The hover request on the broken doc was ANSWERED (not hung).
    hover = [m for m in msgs if m.get("id") == 42]
    if not hover:
        failures.append("hover request (id=42) never got a response — client "
                        "would hang until timeout")
    elif "error" in hover[0]:
        failures.append("hover answered with an internal error: %s"
                        % hover[0]["error"])

    # 3. didClose cleared diagnostics.
    closes = [m for m in msgs
              if m.get("method") == "textDocument/publishDiagnostics"
              and m["params"]["uri"] == "file:///smoke.hls"]
    if not any(len(m["params"]["diagnostics"]) == 0 for m in closes):
        failures.append("didClose did not publish empty diagnostics")

    if failures:
        for f in failures:
            sys.stderr.write("FAIL: %s\n" % f)
        return 1
    sys.stderr.write("lsp_smoke: all protocol assertions passed "
                     "(parse-error resilience, hover on broken doc, "
                     "didClose clears diagnostics, clean exit)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
