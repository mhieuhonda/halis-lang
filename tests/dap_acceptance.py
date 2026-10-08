#!/usr/bin/env python3
"""Stage 121 acceptance gate — VS Code extension debugger integration
(DAP): tools/hls-dap.py, the editors/vscode debug contributions, and
the stage's ok corpus file.

Run with `make dap-acceptance` (or
`python3 tests/dap_acceptance.py`).

Nine sections, every one driving the REAL adapter as a subprocess over
stdio DAP (the same pipe VS Code opens):

  1. the lifecycle   — initialize answers capabilities (configuration
                       done, evaluate-for-hovers, the panics exception
                       filter), launch rejects a missing program and a
                       program the checker rejects with the REAL
                       diagnostic, configurationDone starts the run,
                       and a clean program ends exited(0)+terminated
  2. the breakpoints — a statement line verifies in place, a line
                       without a statement snaps DOWN to the nearest
                       statement and says so, a line past the end is
                       refused, a file outside the program is refused;
                       a hit stops with reason breakpoint on the line
                       that is ABOUT to execute, a loop hits it once
                       per iteration, and clearing the breakpoints lets
                       the program run out
  3. the output      — the program's stdout arrives as output events,
                       byte-for-byte the same bytes boot.py prints
  4. the stack       — stackTrace lists innermost first, every frame
                       names its fn and its real source file, and the
                       top frame sits on the stopped line
  5. the stepping    — stopOnEntry stops at main's first statement
                       before it runs; next crosses the loop; stepIn
                       descends into the callee (two levels, through
                       the method); stepOut climbs back out
  6. the variables   — Locals binds every visible name with the right
                       value and declared type; a struct expands to its
                       declared fields; an enum shows Variant(payload);
                       a list indexes 0..n-1; a shadowed name reports
                       its shadow
  7. the evaluate    — arithmetic over real locals, a bare identifier,
                       a struct field chain; a parse error, an unknown
                       name and a panicking expression are error
                       responses that leave the session alive
  8. the panic       — a bounds panic stops the dead program with
                       reason exception, the real message, the panic
                       line, the stderr echo, exit 101; with the
                       panics filter off the same program just exits
  9. the integration — the editor contributes the `halis` debugger
                       type (launch attributes, snippets), extension.js
                       registers a descriptor factory pointing at the
                       adapter, and the corpus file is hlfmt-canonical,
                       hllint-clean, and runs under boot with its
                       pinned output

Exit code 0 = all acceptance criteria met.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

ADAPTER = os.path.join(ROOT, "tools", "hls-dap.py")
CORPUS = os.path.join(ROOT, "tests", "ok", "feat_stage121_dap.hls")
EXT = os.path.join(ROOT, "editors", "vscode", "halis")
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]
PY = sys.executable

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s121_", dir=os.path.join(ROOT, "tests"))


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
# a minimal but honest DAP client: real framing, seq tracking, and
# helpers that fail LOUD on timeout (a hung assertion is a failed one)
# ---------------------------------------------------------------------------

class DapClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            [PY, ADAPTER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.buf = b""
        self.msgs = []
        self.dead = False
        self.stopped_seen = 0
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        while True:
            b = self.proc.stdout.read(1)
            if not b:
                self.dead = True
                break
            with self.lock:
                self.buf += b
                self._drain()

    def _drain(self):
        while True:
            if b"Content-Length:" not in self.buf:
                return
            i = self.buf.index(b"Content-Length:")
            head = self.buf[i:]
            if b"\r\n\r\n" not in head:
                return
            j = self.buf.index(b"\r\n\r\n", i)
            try:
                n = int(self.buf[i + 16:j])
            except ValueError:
                return
            if j + 4 + n > len(self.buf):
                return
            body = self.buf[j + 4:j + 4 + n]
            self.buf = self.buf[j + 4 + n:]
            try:
                self.msgs.append(json.loads(body))
            except ValueError:
                pass

    def send(self, obj):
        body = json.dumps(obj).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n%s"
                              % (len(body), body))
        self.proc.stdin.flush()

    def request(self, command, args=None):
        seq = self.next_seq()
        msg = {"seq": seq, "type": "request", "command": command}
        if args is not None:
            msg["arguments"] = args
        self.send(msg)
        return seq

    _seq = 0

    def next_seq(self):
        DapClient._seq += 1
        return DapClient._seq

    def response(self, seq, timeout=15):
        return self._wait(lambda m: m.get("type") == "response"
                          and m.get("request_seq") == seq,
                          "response %d" % seq, timeout)

    def event(self, name, timeout=15):
        return self._wait(lambda m: m.get("type") == "event"
                          and m.get("event") == name,
                          "event %s" % name, timeout)

    def events(self, name):
        return [m for m in list(self.msgs)
                if m.get("type") == "event" and m.get("event") == name]

    def _wait(self, pred, what, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in list(self.msgs):
                if pred(m):
                    return m
            if self.dead:
                break
            time.sleep(0.02)
        raise AssertionError("timed out waiting for %s" % what)

    def rpc(self, command, args=None, timeout=15):
        seq = self.request(command, args)
        return self.response(seq, timeout)

    def wait_stopped(self, reason=None, timeout=15):
        """Wait for the NEXT stopped event (events already consumed by
        earlier calls are never returned again)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            stops = [m for m in list(self.msgs)
                     if m.get("type") == "event"
                     and m.get("event") == "stopped"]
            for ev in stops[self.stopped_seen:]:
                if reason is None or ev["body"]["reason"] == reason:
                    self.stopped_seen = stops.index(ev) + 1
                    return ev
            if self.dead:
                break
            time.sleep(0.02)
        raise AssertionError("timed out waiting for stopped(%s)"
                             % (reason or "any"))

    def stdout_text(self):
        return "".join(m["body"].get("output", "")
                       for m in self.events("output")
                       if m["body"].get("category") == "stdout")

    def stderr_text(self):
        return "".join(m["body"].get("output", "")
                       for m in self.events("output")
                       if m["body"].get("category") == "stderr")

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def launch_and_run(self, program, launch_args=None, timeout=30):
        """initialize → launch → configurationDone → wait for the
        session to end; returns (client, events-after-launch)."""
        self.rpc("initialize", {"adapterID": "acceptance"})
        args = {"program": program}
        if launch_args:
            args.update(launch_args)
        self.rpc("launch", args)
        self.rpc("configurationDone")
        self.event("terminated", timeout)
        return self


def top_frame(cl, thread_id):
    st = cl.rpc("stackTrace", {"threadId": thread_id})
    return st["body"]["stackFrames"][0]


def locals_of(cl, frame_id):
    sc = cl.rpc("scopes", {"frameId": frame_id})
    scopes = sc["body"]["scopes"]
    if not scopes:
        return []
    ref = scopes[0]["variablesReference"]
    vv = cl.rpc("variables", {"variablesReference": ref})
    return vv["body"]["variables"]


# ---------------------------------------------------------------------------
# 1. lifecycle
# ---------------------------------------------------------------------------

def section_lifecycle():
    print("\n[1] lifecycle: initialize, launch errors, clean exit")

    cl = DapClient()
    r = cl.rpc("initialize", {"adapterID": "acceptance"})
    caps = r["body"]
    check(caps.get("supportsConfigurationDoneRequest") is True,
          "initialize: configurationDone supported")
    check(caps.get("supportsEvaluateForHovers") is True,
          "initialize: evaluate for hovers supported")
    filters = caps.get("exceptionBreakpointFilters") or []
    check(any(f.get("filter") == "panics" for f in filters),
          "initialize: panics exception filter advertised")
    check(any(f.get("default") is True for f in filters),
          "initialize: panics filter defaults on")
    cl.event("initialized")
    ok("initialize: initialized event follows the response")

    r = cl.rpc("launch", {"program": os.path.join(TMP, "nope.hls")})
    check(r["success"] is False and "not found" in (r.get("message") or ""),
          "launch: missing program refused (%s)" % r.get("message"))

    bad_prog = write("fail_type.hls",
                     "fn main() {\n    let x: int = \"s\"\n}\n")
    r = cl.rpc("launch", {"program": bad_prog})
    check(r["success"] is False,
          "launch: a program the checker rejects is refused")
    check("type" in (r.get("message") or "").lower()
          or "mismatch" in (r.get("message") or "").lower(),
          "launch: the refusal carries the real diagnostic (%s)"
          % r.get("message"))

    good = write("hello.hls",
                 'fn main() uses IO {\n    println("ok")\n}\n')
    r = cl.rpc("launch", {"program": good})
    check(r["success"] is True, "launch: a valid program launches")

    cl.rpc("setExceptionBreakpoints", {"filters": ["panics"]})
    ok("setExceptionBreakpoints: panics filter accepted")

    cl.rpc("configurationDone")
    cl.event("terminated")
    exited = cl.events("exited")
    check(exited and exited[-1]["body"]["exitCode"] == 0,
          "lifecycle: clean program exits 0 after terminated ordering")
    cl.close()
    check(cl.proc.poll() == 0,
          "lifecycle: closing stdin ends the adapter cleanly (exit %s)"
          % cl.proc.poll())


# ---------------------------------------------------------------------------
# 2. breakpoints
# ---------------------------------------------------------------------------

def section_breakpoints():
    print("\n[2] breakpoints: verify, snap, refuse, hit, clear")

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": CORPUS})

    # corpus line numbers (stable, pinned by hlfmt): 49 is the
    # stock_value signature (no statement on it), 54 is `i = i + 1`
    # inside its loop, the file ends at line 118.
    r = cl.rpc("setBreakpoints", {"source": {"path": CORPUS},
                                  "breakpoints": [{"line": 49},
                                                  {"line": 5000}]})
    bps = r["body"]["breakpoints"]
    check(len(bps) == 2, "setBreakpoints: two breakpoints answered")
    check(bps[0]["verified"] is True and bps[0]["line"] == 50,
          "setBreakpoints: line 49 (fn signature) snaps to the next "
          "statement at line 50")
    check("snap" in (bps[0].get("message") or ""),
          "setBreakpoints: the snap says so in its message")
    check(bps[1]["verified"] is False,
          "setBreakpoints: a line past the end is refused")

    r = cl.rpc("setBreakpoints",
               {"source": {"path": os.path.join(TMP, "stranger.hls")},
                "breakpoints": [{"line": 1}]})
    check(r["body"]["breakpoints"][0]["verified"] is False,
          "setBreakpoints: a file outside the program is refused")

    # replace the set with the loop line only, then hit it 3 times
    r = cl.rpc("setBreakpoints", {"source": {"path": CORPUS},
                                  "breakpoints": [{"line": 54}]})
    check(r["body"]["breakpoints"][0]["verified"] is True
          and r["body"]["breakpoints"][0]["line"] == 54,
          "setBreakpoints: the loop line verifies in place")

    cl.rpc("configurationDone")
    lines = []
    for _ in range(3):
        ev = cl.wait_stopped("breakpoint")
        lines.append(top_frame(cl, ev["body"]["threadId"])["line"])
        cl.rpc("continue", {"threadId": ev["body"]["threadId"]})
    check(lines == [54, 54, 54],
          "breakpoint: the loop hits it once per iteration (%r)" % lines)

    # clear breakpoints; the program must run out
    cl.rpc("setBreakpoints", {"source": {"path": CORPUS},
                              "breakpoints": []})
    cl.event("terminated")
    check(cl.events("exited")[-1]["body"]["exitCode"] == 0,
          "breakpoint: cleared breakpoints let the program finish")
    cl.close()


# ---------------------------------------------------------------------------
# 3. output parity with boot.py
# ---------------------------------------------------------------------------

def section_output():
    print("\n[3] output: the debug console sees boot.py's bytes")

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": CORPUS, "noDebug": True})
    cl.rpc("configurationDone")
    cl.event("terminated")
    via_dap = cl.stdout_text()
    cl.close()

    direct = subprocess.run(BOOT + [CORPUS], capture_output=True).stdout
    check(via_dap == direct.decode("utf-8"),
          "output: DAP stdout == boot.py stdout (byte for byte)")
    check("value: 636" in via_dap and "loudest: bolt" in via_dap,
          "output: the corpus report lines are all there")


# ---------------------------------------------------------------------------
# 4. the stack
# ---------------------------------------------------------------------------

def section_stack():
    print("\n[4] stack: innermost first, real files, stopped line")

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": CORPUS})
    cl.rpc("setBreakpoints", {"source": {"path": CORPUS},
                              "breakpoints": [{"line": 46}]})
    cl.rpc("configurationDone")
    ev = cl.wait_stopped("breakpoint")
    st = cl.rpc("stackTrace",
                {"threadId": ev["body"]["threadId"]})
    frames = st["body"]["stackFrames"]
    check(st["body"]["totalFrames"] == 3,
          "stack: three frames across two calls (got %d)"
          % st["body"]["totalFrames"])
    check([f["name"] for f in frames] == ["item_value", "stock_value",
                                          "main"],
          "stack: innermost first with real fn names (%r)"
          % [f["name"] for f in frames])
    check([f["line"] for f in frames] == [46, 53, 100],
          "stack: each frame sits on the line it is about to run "
          "(%r)" % [f["line"] for f in frames])
    check(all(f["source"]["path"] == CORPUS for f in frames),
          "stack: every frame names its real source file")
    # a second stackTrace for the same stop agrees (ids stay valid)
    st2 = cl.rpc("stackTrace", {"threadId": ev["body"]["threadId"]})
    check([f["line"] for f in st2["body"]["stackFrames"]]
          == [f["line"] for f in frames],
          "stack: repeated requests agree")
    cl.rpc("disconnect")
    cl.close()


# ---------------------------------------------------------------------------
# 5. stepping
# ---------------------------------------------------------------------------

def section_stepping():
    print("\n[5] stepping: entry, next, stepIn, stepOut")

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": CORPUS, "stopOnEntry": True})
    cl.rpc("configurationDone")
    ev = cl.wait_stopped("entry")
    tid = ev["body"]["threadId"]
    top = top_frame(cl, tid)
    check(top["name"] == "main" and top["line"] == 83,
          "entry: main's first statement, before it runs (line %d)"
          % top["line"])
    locals0 = locals_of(cl, top["id"])
    check(locals0 == [],
          "entry: main has no locals before its first binding")

    # next: cross the multi-line list literal to the next binding
    cl.rpc("next", {"threadId": tid})
    ev = cl.wait_stopped("step")
    top = top_frame(cl, tid)
    check(top["line"] == 94,
          "next: lands on the next statement (line %d)" % top["line"])
    locals1 = {v["name"]: v for v in locals_of(cl, top["id"])}
    check("inv" in locals1,
          "next: the binding from the executed line is now visible")

    cl.rpc("next", {"threadId": tid})
    ev = cl.wait_stopped("step")
    check(top_frame(cl, tid)["line"] == 100,
          "next: reached the call to stock_value (line %d)"
          % top_frame(cl, tid)["line"])

    # stepIn walks statement to statement: into stock_value, through
    # its lets and loop, then into the call on line 53
    cl.rpc("stepIn", {"threadId": tid})
    ev = cl.wait_stopped("step")
    top = top_frame(cl, tid)
    check(top["name"] == "stock_value" and top["line"] == 50,
          "stepIn: descends into stock_value's first statement "
          "(%s line %d)" % (top["name"], top["line"]))
    for want in (51, 52, 53):
        cl.rpc("stepIn", {"threadId": tid})
        ev = cl.wait_stopped("step")
        check(top_frame(cl, tid)["line"] == want,
              "stepIn: statement at line %d" % want)
    cl.rpc("stepIn", {"threadId": tid})
    ev = cl.wait_stopped("step")
    top = top_frame(cl, tid)
    check(top["name"] == "item_value" and top["line"] == 46,
          "stepIn: enters item_value at its return (%s line %d)"
          % (top["name"], top["line"]))
    cl.rpc("stepIn", {"threadId": tid})
    ev = cl.wait_stopped("step")
    top = top_frame(cl, tid)
    check(top["name"] == "Item.value" and top["line"] == 37,
          "stepIn: reaches the impl method through the call chain "
          "(%s line %d)" % (top["name"], top["line"]))
    depth_locals = locals_of(cl, top["id"])
    check(any(v["name"] == "self" for v in depth_locals),
          "stepIn: the method frame binds self")

    # stepOut: Item.value returns, item_value returns, the next
    # statement executed is stock_value's loop line
    cl.rpc("stepOut", {"threadId": tid})
    ev = cl.wait_stopped("step")
    top = top_frame(cl, tid)
    check(top["name"] == "stock_value" and top["line"] == 54,
          "stepOut: climbs back to the caller's next statement "
          "(%s line %d)" % (top["name"], top["line"]))
    cl.rpc("disconnect")
    cl.close()


# ---------------------------------------------------------------------------
# 6. variables
# ---------------------------------------------------------------------------

def section_variables():
    print("\n[6] variables: locals, types, struct/enum/list children")

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": CORPUS})
    # stop after `first` binds (line 116) — the richest main frame
    cl.rpc("setBreakpoints", {"source": {"path": CORPUS},
                              "breakpoints": [{"line": 116}]})
    cl.rpc("configurationDone")
    ev = cl.wait_stopped("breakpoint")
    tid = ev["body"]["threadId"]
    top = top_frame(cl, tid)

    def expand(v):
        if not v.get("variablesReference"):
            return []
        r = cl.rpc("variables",
                   {"variablesReference": v["variablesReference"]})
        return r["body"]["variables"]

    lv = {v["name"]: v for v in locals_of(cl, top["id"])}
    # `k` is scoped to the loop body — out of scope at line 116, and
    # honest debuggers do not invent bindings
    check(set(lv.keys()) == {"inv", "shelves", "j", "first"},
          "variables: every visible binding, block scopes respected "
          "(%r)" % sorted(lv.keys()))
    check(lv["first"]["value"].startswith('Item { name: "rice"'),
          "variables: a struct renders Name { field: v } (%s)"
          % lv["first"]["value"])
    check(lv["first"]["type"] == "Item",
          "variables: the declared type annotation is exact (%s)"
          % lv["first"]["type"])
    check(lv["j"]["value"] == "3" and lv["j"]["type"] == "int",
          "variables: scalars carry values and types (%s)"
          % lv["j"]["value"])

    fields = expand(lv["first"])
    check([(f["name"], f["value"]) for f in fields]
          == [("name", '"rice"'), ("qty", "12"), ("unit", "3")],
          "variables: the struct expands to its declared fields (%r)"
          % [(f["name"], f["value"]) for f in fields])
    check(all(f["type"] in ("str", "int") for f in fields),
          "variables: struct field types come from the declaration")

    shelves = expand(lv["shelves"])
    check(shelves[0]["value"] == "Kind.Food(1)",
          "variables: an enum renders Variant(payload) (%s)"
          % shelves[0]["value"])
    check(shelves[0]["type"] == "Kind",
          "variables: the enum annotation names the enum (%s)"
          % shelves[0]["type"])
    payload = expand(shelves[0])
    check(payload and payload[0]["name"] == "0"
          and payload[0]["value"] == "1",
          "variables: enum payloads expand as indexed children")

    inv = expand(lv["inv"])
    check([v["name"] for v in inv] == ["0", "1", "2"],
          "variables: a list indexes 0..n-1 (%r)"
          % [v["name"] for v in inv])
    inner = expand(inv[1])
    check([f["value"] for f in inner] == ['"drill"', "4", "25"],
          "variables: nested structs expand through the index (%r)"
          % [f["value"] for f in inner])

    # evaluate a struct and expand it from the REPL result
    r = cl.rpc("evaluate", {"frameId": top["id"], "expression": "inv[0]",
                            "context": "repl"})
    ev_rows = expand(r["body"])
    check(r["success"] and [f["name"] for f in ev_rows]
          == ["name", "qty", "unit"],
          "evaluate: a struct result expands in the console")
    cl.rpc("disconnect")
    cl.close()


# ---------------------------------------------------------------------------
# 7. evaluate
# ---------------------------------------------------------------------------

def section_evaluate():
    print("\n[7] evaluate: real expressions, real errors, no deaths")

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": CORPUS})
    cl.rpc("setBreakpoints", {"source": {"path": CORPUS},
                              "breakpoints": [{"line": 116}]})
    cl.rpc("configurationDone")
    ev = cl.wait_stopped("breakpoint")
    tid = ev["body"]["threadId"]
    fid = top_frame(cl, tid)["id"]

    def evl(expr):
        return cl.rpc("evaluate", {"frameId": fid, "expression": expr,
                                   "context": "repl"})

    r = evl("1 + 2 * 3")
    check(r["success"] and r["body"]["result"] == "7",
          "evaluate: arithmetic over literals (%s)"
          % r.get("body", {}).get("result"))
    r = evl("j * 2")
    check(r["success"] and r["body"]["result"] == "6",
          "evaluate: arithmetic over REAL locals (%s)"
          % r.get("body", {}).get("result"))
    r = evl('first.name + "!"')
    check(r["success"] and r["body"]["result"] == '"rice!"',
          "evaluate: a struct field chain (%s)"
          % r.get("body", {}).get("result"))
    r = evl("first.qty * first.unit")
    check(r["success"] and r["body"]["result"] == "36",
          "evaluate: computed from the paused frame (%s)"
          % r.get("body", {}).get("result"))
    r = evl('len("abc")')
    check(r["success"] and r["body"]["result"] == "3",
          "evaluate: builtins run too (%s)"
          % r.get("body", {}).get("result"))

    r = evl("1 +")
    check(r["success"] is False, "evaluate: a parse error is an error")
    r = evl("no_such_var")
    check(r["success"] is False and "no_such_var" in (r.get("message") or ""),
          "evaluate: an unknown name is an error naming it (%s)"
          % r.get("message"))
    r = evl("[1, 2] + 1")
    check(r["success"] is False,
          "evaluate: a panicking expression is an error, not a crash")

    # the session survived every bad expression: finish it cleanly
    cl.rpc("continue", {"threadId": tid})
    cl.event("terminated")
    check(cl.events("exited")[-1]["body"]["exitCode"] == 0,
          "evaluate: the session stays alive after errors and ends 0")
    cl.close()


# ---------------------------------------------------------------------------
# 8. panics
# ---------------------------------------------------------------------------

PANIC_SRC = """fn boom(xs: list[int]) -> int {
    return xs[9]
}

fn main() uses IO {
    println("before")
    let v: int = boom([1, 2, 3])
    println("after " + v.to_str())
}
"""


def section_panic():
    print("\n[8] panics: stop dead at the site, then just exit")

    prog = write("panic_prog.hls", PANIC_SRC)

    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": prog})
    cl.rpc("setExceptionBreakpoints", {"filters": ["panics"]})
    cl.rpc("configurationDone")
    ev = cl.wait_stopped("exception")
    tid = ev["body"]["threadId"]
    check(ev["body"]["text"] == "array access out of bounds",
          "panic: the stopped event carries the real message (%s)"
          % ev["body"].get("text"))
    top = top_frame(cl, tid)
    check(top["name"] == "boom" and top["line"] == 2,
          "panic: the stack is parked at the site (boom line %d)"
          % top["line"])
    check("before" in cl.stdout_text() and "after" not in cl.stdout_text(),
          "panic: output stops where the program died")
    cl.rpc("continue", {"threadId": tid})
    cl.event("terminated")
    exited = cl.events("exited")
    check(exited and exited[-1]["body"]["exitCode"] == 101,
          "panic: the session ends with exit 101")
    check("panic:" in cl.stderr_text(),
          "panic: the stderr echo reaches the console")
    cl.close()

    # filter off: the same program just dies
    cl = DapClient()
    cl.rpc("initialize", {"adapterID": "acceptance"})
    cl.rpc("launch", {"program": prog})
    cl.rpc("setExceptionBreakpoints", {"filters": []})
    cl.rpc("configurationDone")
    ev = cl.event("terminated")
    exited = cl.events("exited")
    check(exited and exited[-1]["body"]["exitCode"] == 101,
          "panic: filter off — no stop, exit 101 directly")
    check(not cl.events("stopped"),
          "panic: filter off — no stopped event")
    cl.close()


# ---------------------------------------------------------------------------
# 9. protocol robustness + editor integration + corpus hygiene
# ---------------------------------------------------------------------------

def section_integration():
    print("\n[9] integration: protocol, editor contributions, corpus")

    # --- protocol robustness: garbage in, service out ----------------
    cl = DapClient()
    cl.proc.stdin.write(b"this is not a DAP frame at all\n")
    cl.proc.stdin.flush()
    r = cl._wait(lambda m: m.get("type") == "response"
                 and "parse error" in (m.get("message") or ""),
                 "parse-error response", 10)
    ok("protocol: a garbage frame earns a parse-error response")

    body = b"{not json"
    cl.proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    cl.proc.stdin.flush()
    cl._wait(lambda m: m.get("type") == "response"
             and "invalid JSON" in (m.get("message") or ""),
             "bad-JSON response", 10)
    ok("protocol: an invalid JSON body earns a parse error too")

    r = cl.rpc("frobnicate", {})
    check(r["success"] is False and "unknown request" in r["message"],
          "protocol: an unknown request is refused by name")

    # the adapter still serves a full session after all of that
    r = cl.rpc("initialize", {"adapterID": "acceptance"})
    check(r["success"], "protocol: the session continues after garbage")
    prog = write("hello9.hls", 'fn main() uses IO {\n    println("x")\n}\n')
    cl.rpc("launch", {"program": prog})
    cl.rpc("configurationDone")
    cl.event("terminated")
    ok("protocol: a program still runs to completion")
    cl.close()

    # --- corpus hygiene ----------------------------------------------
    out = subprocess.run(BOOT + [CORPUS], capture_output=True)
    pinned = ("value: 636\nloudest: bolt\nshelf 1 kind food\n"
              "shelf 7 kind tool\nshelf 9 kind spare\n"
              "first: rice x12 value 36\n")
    check(out.returncode == 0
          and out.stdout.decode("utf-8") == pinned,
          "corpus: boot.py runs it with the pinned output")
    fmt = subprocess.run([PY, "tools/hlfmt.py", "-c", CORPUS],
                         capture_output=True)
    check(fmt.returncode == 0, "corpus: hlfmt -c holds it canonical")
    lint = subprocess.run([PY, "tools/hllint.py", "--strict", CORPUS],
                          capture_output=True)
    check(lint.returncode == 0, "corpus: hllint --strict is clean")

    # --- editor contributions ---------------------------------------
    pkg = json.load(open(os.path.join(EXT, "package.json")))
    dbg = (pkg.get("contributes") or {}).get("debuggers") or []
    if not check(len(dbg) == 1 and dbg[0].get("type") == "halis",
                 "editor: package.json contributes the `halis` debugger "
                 "type"):
        return
    dbg = dbg[0]
    attrs = (dbg.get("configurationAttributes") or {}).get("launch") or {}
    check("program" in (attrs.get("required") or []),
          "editor: launch configuration requires `program`")
    props = attrs.get("properties") or {}
    check("args" in props and "stopOnEntry" in props,
          "editor: launch attributes cover args and stopOnEntry")
    check(bool(dbg.get("configurationSnippets")),
          "editor: launch.json snippets are provided")
    check("halis.startDebugging" in
          json.dumps(pkg.get("contributes", {}).get("commands", [])),
          "editor: a Start Debugging command is contributed")
    ext_js = open(os.path.join(EXT, "extension.js")).read()
    check("registerDebugAdapterDescriptorFactory" in ext_js,
          "editor: extension.js registers a descriptor factory")
    check("hls-dap.py" in ext_js,
          "editor: the factory points at tools/hls-dap.py")
    check("DebugAdapterExecutable" in ext_js,
          "editor: the factory launches the adapter over stdio")


# ---------------------------------------------------------------------------

def main():
    print("Stage 121 acceptance — VS Code extension debugger (DAP)")
    try:
        section_lifecycle()
        section_breakpoints()
        section_output()
        section_stack()
        section_stepping()
        section_variables()
        section_evaluate()
        section_panic()
        section_integration()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n==========================================")
    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    print("==========================================")
    if FAIL:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
