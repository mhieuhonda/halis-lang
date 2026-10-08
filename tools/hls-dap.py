#!/usr/bin/env python3
"""hls-dap — Debug Adapter Protocol server for Halis (HLS).

Stage 121 (v0.140.0-alpha): VS Code extension debugger integration (DAP).

The adapter speaks the Debug Adapter Protocol over stdio (the same
Content-Length framing the language server uses), so any DAP client —
VS Code first — can debug a Halis program through the Stage-0
interpreter: the same reference implementation boot.py runs.

Design contract (the house discipline: never guess):

  * The program is PARSED AND CHECKED FIRST. A program the compiler
    rejects never starts debugging: the launch request fails with the
    real checker diagnostic, the same text boot.py prints.
  * Execution runs in a session thread under a thin hook layer wrapped
    AROUND the untouched interpreter (`exec_stmt` reports statements,
    `call_fn` maintains the frame stack). The interpreter itself is
    not modified — differential behaviour, bootstrap, and every suite
    stay exactly as they were.
  * A stop is a real pause: the interpreter thread blocks on a
    condition variable and nothing of the program mutates while the
    variables view reads it.
  * Breakpoints are VERIFIED against the statement lines of the real
    AST: a line without a statement snaps DOWN to the next line that
    has one (the response says so), and a breakpoint in a file the
    program does not contain is refused (unverified) — statement to
    file attribution is exact, resolved per function through the
    import tree, so a line number in one file can never stop another
    file's execution.
  * Expressions typed in the debug console (and hovers) are evaluated
    by the REAL parser in the paused frame's real environment, with
    stepping suppressed while evaluation runs; a panic in the
    expression is an error result, never a dead session.

Requests: initialize, launch, setBreakpoints, setExceptionBreakpoints,
configurationDone, threads, stackTrace, scopes, variables, evaluate,
continue, next, stepIn, stepOut, pause, disconnect, terminate.
Events: initialized, process, output, stopped, exited, terminated.

Launch arguments:
  program (required)     path to the .hls entry file
  args                   list of program arguments (argv[1:])
  stopOnEntry            break at the first statement of main
  noDebug                run without stopping (output still forwarded)

Usage: python3 tools/hls-dap.py            (DAP over stdio)

Known limits (Stage-0 reference debugger): expression evaluation may
run user calls (its effects are real); a task blocked inside a channel
builtin reports the line of its last executed statement; `asm!`
statements panic under the interpreter exactly as boot.py documents.
"""
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boot.boot import load_program, _resolve_import     # noqa: E402
from boot.checker import check                          # noqa: E402
from boot.interp import Interp, HLPanic                 # noqa: E402
from boot.lexer import tokenize, HLError                # noqa: E402
from boot.parser import Parser                          # noqa: E402
from boot.interp_parts.exec import InterpExec           # noqa: E402
from boot.interp_parts.core import InterpCore           # noqa: E402

DAP_VERSION = "1.64.0"
TOOL_VERSION = "0.140.0-alpha"

# Statement kinds the interpreter actually executes (boot/interp_parts/
# exec.py exec_stmt). The breakpoint verifier accepts exactly these.
STMT_KINDS = frozenset((
    "let", "assign", "if", "while", "for",
    "return", "break", "continue", "expr", "asm",
))


class BadFrame(Exception):
    """A DAP framing violation (garbage header / bad length)."""


# ---------------------------------------------------------------------------
# value display — one formatter, used by variables, evaluate and hovers
# ---------------------------------------------------------------------------

def _shorten(text, cap=220):
    if len(text) <= cap:
        return text
    return text[:cap] + "…"


def quote_str(s):
    return _shorten(json.dumps(s))


def display_value(v, ty=None):
    """Render a runtime value the way Halis means it.

    bools print as true/false (the HLS literals, not Python's), bytes
    decode as UTF-8 (strings live as bytes inside the interpreter),
    structs render `Name { field: v, ... }`, enums render
    `Variant(payload)` — the same shape the parser accepts.
    """
    if v is None:
        return "void"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return quote_str(v.decode("utf-8", "replace"))
    if isinstance(v, str):
        return quote_str(v)
    if isinstance(v, list):
        head = ", ".join(display_value(e) for e in v[:6])
        if len(v) > 6:
            head += ", …"
        return "[%s] (len=%d)" % (head, len(v))
    if isinstance(v, dict):
        if v.get("enum") is not None and "var" in v:
            data = v.get("data") or []
            name = "%s.%s" % (v["enum"], v["var"])
            if data:
                return _shorten("%s(%s)" % (
                    name, ", ".join(display_value(d) for d in data)))
            return name
        if v.get("tainted"):
            return "tainted[%s]" % display_value(v.get("value"))
        items = ", ".join("%s: %s" % (k, display_value(x))
                          for k, x in list(v.items())[:6])
        if len(v) > 6:
            items += ", …"
        if ty and _struct_name(ty):
            return "%s { %s }" % (_struct_name(ty), items)
        return "{ %s }" % items if v else "{}"
    return str(v)


def display_type(v, ty=None):
    """Best-effort type annotation: the declared type when it is
    consistent with the runtime shape, else a structural name, else
    '' (honest unknown)."""
    if v is None:
        return "void"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (bytes, str)):
        return "str"
    if isinstance(v, list):
        if ty and ty.startswith("list["):
            return ty
        return "list"
    if isinstance(v, dict):
        if v.get("enum") is not None and "var" in v:
            return v["enum"]
        if v.get("tainted"):
            return ty or "tainted"
        if ty and _struct_name(ty):
            return ty
        return "map"
    return ty or ""


def _struct_name(ty):
    """A type string that names a plain struct (`Item`, `Box[int]`) —
    generic instantiations keep the base name visible; builtins are
    excluded by the caller."""
    if not ty or "[" in ty or ty in ("int", "float", "bool", "str", "void",
                                     "list", "map"):
        return None
    return ty


# ---------------------------------------------------------------------------
# static program info — statement lines, fn->file attribution, var types
# ---------------------------------------------------------------------------

def collect_stmt_lines(stmts, out):
    """Recursively collect the executable statement lines of a body."""
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k in STMT_KINDS:
            out.add(s.get("line", 0))
        if k == "if":
            collect_stmt_lines(s.get("then") or [], out)
            els = s.get("els")
            if els:
                collect_stmt_lines(els, out)
        elif k in ("while", "for"):
            collect_stmt_lines(s.get("body") or [], out)


def collect_var_types(stmts, vt):
    """Collect declared binding types across a fn body. Later bindings
    win (a shadowing sibling block may lend its type to the display;
    values are always the real runtime values, and display_type never
    trusts a declaration inconsistent with the value's shape)."""
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "let":
            vt[s["name"]] = s.get("t")
        elif k == "if":
            collect_var_types(s.get("then") or [], vt)
            if s.get("els"):
                collect_var_types(s["els"], vt)
        elif k in ("while", "for"):
            collect_var_types(s.get("body") or [], vt)


class ProgramInfo:
    """Everything static the debugger needs: which fn lives in which
    file, which lines hold statements, what the bindings are declared
    as. Built from the real parser output — nothing is inferred from
    text heuristics."""

    def __init__(self, program, entry_path):
        self.program = program
        self.entry = os.path.realpath(os.path.abspath(entry_path))
        self.fn_file = {}        # fn key -> file path
        self.fn_names = {}       # fn key -> display name
        self.file_lines = {}     # file path -> set of statement lines
        self._var_types = {}     # fn key -> {name: declared type}
        self._lock = threading.Lock()
        self._scan()

    def _scan(self):
        entry = self.entry
        files = [entry]
        seen = {entry}
        i = 0
        # BFS over the import tree, resolving through the SAME import
        # resolver boot.py uses — the debugger can never disagree with
        # the build about what the program contains.
        while i < len(files):
            cur = files[i]
            i += 1
            try:
                with open(cur, "rb") as f:
                    src = f.read()
                prog = Parser(tokenize(src), src).parse_program()
            except (OSError, HLError):
                continue  # already reported by load_program's own pass
            lines = self.file_lines.setdefault(cur, set())
            for fdef in prog["fns"].values():
                collect_stmt_lines(fdef["body"], lines)
            for imp in prog.get("imports", []):
                resolved = _resolve_import(imp["path"], cur)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    files.append(resolved)
        # Attribution: a fn belongs to the file(s) that declare it. The
        # merge step of load_program guarantees global key uniqueness,
        # so the first file that declares a key is ITS file.
        for path in files:
            try:
                with open(path, "rb") as f:
                    src = f.read()
                prog = Parser(tokenize(src), src).parse_program()
            except (OSError, HLError):
                continue
            for key, fdef in prog["fns"].items():
                self.fn_file.setdefault(key, path)
                # the KEY is the display name: top-level fns print bare,
                # impl methods print Struct.method — the same spelling
                # the compiler and the LSP use
                self.fn_names[key] = key

    def stmt_lines(self, path):
        real = os.path.realpath(os.path.abspath(path))
        return self.file_lines.get(real)

    def is_program_file(self, path):
        real = os.path.realpath(os.path.abspath(path))
        return real in self.file_lines

    def var_types(self, key):
        with self._lock:
            vt = self._var_types.get(key)
            if vt is None:
                vt = {}
                fn = self.program["fns"].get(key)
                if fn is not None:
                    for (pn, pty, _m) in fn["params"]:
                        vt[pn] = pty
                    collect_var_types(fn["body"], vt)
                self._var_types[key] = vt
            return vt


# ---------------------------------------------------------------------------
# the debug engine — hooks around the interpreter, one stop discipline
# ---------------------------------------------------------------------------

_HOOK_LOCK = threading.Lock()
_ORIG_EXEC_STMT = None
_ORIG_CALL_FN = None


class DebugEngine:
    """Per-session debug state. The interpreter reaches it through two
    module-level hooks installed once per process (exec_stmt, call_fn)
    that delegate into the engine of the Interp being debugged via
    `interp._dbg`. Nothing in boot/ is modified.

    Stop discipline: the interpreter thread runs with `mode == "run"`;
    on a stop decision it snapshots its frame stack, publishes the
    session's pending stdout, emits the stopped event, and blocks on
    the engine's condition variable until the adapter hands it a
    resume command. While paused, NOTHING of the program executes, so
    the variables view reads live structures safely."""

    def __init__(self, session, info, no_debug=False):
        self.session = session
        self.info = info
        self.no_debug = no_debug
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        # tid -> {"mode": "run"|"paused", "cmd": None|(verb, depth),
        #         "snap": [frame dicts], "reason": str,
        #         "entry_pending": bool, "pause_req": bool}
        self.states = {}
        self.tls = threading.local()
        self.breakpoints = {}       # file path -> set of lines
        self.stop_on_panic = True   # exception-breakpoint filter state
        self.in_eval = 0            # >0: evaluation runs, hooks stand down
        self.main_tid = None
        # handle allocation (monotonic, never reused within a session)
        self.next_id = 1
        self.generation = 0
        self.frame_handles = {}     # frameId -> (tid, index into snap)
        self.var_handles = {}       # varRef -> handle record
        self.thread_names = {}      # ident -> "task-N"
        self.panic = None           # first (deepest) panic record
        self.exit_code = None
        # ids the UI sees for threads: 1 is always main
        self.ident_tid = {}         # ident -> dap thread id

    # ---- hook plumbing ------------------------------------------------

    def install(self, interp):
        interp._dbg = self
        global _ORIG_EXEC_STMT, _ORIG_CALL_FN
        with _HOOK_LOCK:
            if _ORIG_EXEC_STMT is None:
                _ORIG_EXEC_STMT = InterpExec.exec_stmt
                _ORIG_CALL_FN = InterpCore.call_fn

                def exec_stmt(self_i, s, env):
                    dbg = getattr(self_i, "_dbg", None)
                    if dbg is None:
                        return _ORIG_EXEC_STMT(self_i, s, env)
                    dbg.hook_stmt(self_i, s, env)
                    try:
                        return _ORIG_EXEC_STMT(self_i, s, env)
                    except HLPanic as ex:
                        if dbg.in_eval == 0:
                            dbg.record_panic(self_i, ex)
                        raise

                def call_fn(self_i, key, args):
                    dbg = getattr(self_i, "_dbg", None)
                    if dbg is None:
                        return _ORIG_CALL_FN(self_i, key, args)
                    dbg.hook_enter(self_i, key)
                    try:
                        return _ORIG_CALL_FN(self_i, key, args)
                    finally:
                        dbg.hook_leave(self_i)

                InterpExec.exec_stmt = exec_stmt
                InterpCore.call_fn = call_fn

    def hook_enter(self, interp, key):
        if self.in_eval:
            return
        frames = getattr(self.tls, "frames", None)
        if frames is None:
            frames = []
            self.tls.frames = frames
        frames.append({
            "fn": key,
            "name": self.info.fn_names.get(key, key),
            "file": self.info.fn_file.get(key),
            "line": 0,
            "env": None,
        })
        # register the thread so breakpoints/pauses reach it and the
        # threads request can see it
        with self.lock:
            if threading.get_ident() not in self.states:
                self._state(threading.get_ident())

    def hook_leave(self, interp):
        frames = getattr(self.tls, "frames", None)
        if frames:
            frames.pop()

    def hook_stmt(self, interp, s, env):
        """The one stop checkpoint: every executed statement passes
        here BEFORE it runs (a breakpoint stop shows the line that is
        ABOUT to execute)."""
        if self.in_eval:
            return
        frames = getattr(self.tls, "frames", None)
        if not frames:
            return
        tid = threading.get_ident()
        top = frames[-1]
        line = s.get("line", 0)
        top["line"] = line
        top["env"] = env
        with self.lock:
            st = self.states.get(tid)
            if st is None or st["mode"] != "run":
                return
            reason = None
            if st.get("entry_pending"):
                st["entry_pending"] = False
                reason = "entry"
            elif not self.no_debug:
                file = top.get("file")
                if file is not None and line in self.breakpoints.get(file, ()):
                    reason = "breakpoint"
            if reason is None and not self.no_debug:
                cmd = st.get("cmd")
                depth = len(frames)
                if cmd is not None:
                    verb, d = cmd
                    if verb == "in":
                        reason = "step"
                    elif verb == "over" and depth <= d:
                        reason = "step"
                    elif verb == "out" and depth < d:
                        reason = "step"
                if reason is None and st.get("pause_req"):
                    st["pause_req"] = False
                    reason = "pause"
            if reason is None:
                return
            self._go_paused(tid, st, frames, reason, line)

    def _go_paused(self, tid, st, frames, reason, line):
        snap = [dict(f) for f in frames]
        if reason != "entry" and snap:
            snap[-1]["line"] = line
        st["mode"] = "paused"
        st["cmd"] = None
        st["snap"] = snap
        st["reason"] = reason
        self.session.flush_output()
        self.session.emit_stopped(tid, reason)
        while True:
            cmd = st.get("cmd")
            if cmd is not None:
                break
            self.cv.wait()
        st["mode"] = "run"

    def record_panic(self, interp, ex):
        """First record wins: unwinding visits the DEEPEST frame first,
        so the first statement-level catch is the panic site."""
        if self.panic is not None:
            return
        frames = getattr(self.tls, "frames", None) or []
        snap = [dict(f) for f in frames]
        if snap:
            snap[-1]["line"] = ex.line
        self.panic = {"tid": threading.get_ident(), "frames": snap,
                      "msg": ex.msg if isinstance(ex.msg, str)
                      else to_display_str(ex.msg),
                      "line": ex.line}

    # ---- resume commands (adapter thread) -----------------------------

    def _state(self, tid):
        st = self.states.get(tid)
        if st is None:
            st = {"mode": "run", "cmd": None, "snap": None,
                  "reason": None, "entry_pending": False,
                  "pause_req": False}
            self.states[tid] = st
        return st

    def set_entry_stop(self):
        with self.lock:
            st = self._state(self.main_tid)
            st["entry_pending"] = True

    def resume_continue(self, tid):
        """continue resumes EVERY paused thread (the tasks are one
        program: a selective continue is how a debugger manufactures
        the very deadlock the program was written not to have)."""
        with self.lock:
            resumed = False
            for st in self.states.values():
                if st["mode"] == "paused":
                    st["cmd"] = ("continue", 0)
                    resumed = True
            self.cv.notify_all()
            return resumed

    def resume_step(self, tid, verb):
        with self.lock:
            st = self._state(tid)
            if st["mode"] != "paused":
                return False
            snap = st.get("snap") or []
            st["cmd"] = (verb, len(snap))
            self.cv.notify_all()
            return True

    def request_pause(self, tid):
        with self.lock:
            st = self._state(tid)
            st["pause_req"] = True
            return st["mode"] == "run"

    def is_paused(self, tid):
        with self.lock:
            st = self.states.get(tid)
            return st is not None and st["mode"] == "paused"

    def snapshot(self, tid):
        with self.lock:
            st = self.states.get(tid)
            if st is not None and st["mode"] == "paused":
                return st["snap"], st["reason"]
        return None, None

    def wait_panic_resume(self, tid):
        """After a panic stop the program is already dead; the session
        thread parks here so the UI can read the stack, and leaves on
        continue/terminate."""
        with self.lock:
            st = self._state(tid)
            while st.get("cmd") is None:
                self.cv.wait()
            st["cmd"] = None
            st["mode"] = "run"

    # ---- handles -------------------------------------------------------

    def fresh_id(self):
        with self.lock:
            i = self.next_id
            self.next_id += 1
            return i

    def new_generation(self):
        with self.lock:
            self.generation += 1
            self.frame_handles = {}
            self.var_handles = {}

    def generation_now(self):
        with self.lock:
            return self.generation

    def register_frames(self, tid, snap):
        for i in range(len(snap)):
            self.frame_handles[self.fresh_id()] = (tid, i)

    def register_var(self, record):
        ref = self.fresh_id()
        self.var_handles[ref] = record
        return ref

    def dap_thread_id(self, ident):
        """Stable DAP thread id per interpreter thread; 1 = main."""
        with self.lock:
            t = self.ident_tid.get(ident)
            if t is not None:
                return t
            if ident == self.main_tid:
                t = 1
            else:
                t = self.fresh_id()
                self.thread_names[ident] = "task-%d" % (t - 1)
            self.ident_tid[ident] = t
            return t

    def live_threads(self):
        """DAP thread list: main always; every live thread that has
        executed at least one statement (a task blocked in a channel
        builtin still owns its call stack)."""
        out = []
        with self.lock:
            for th in threading.enumerate():
                ident = th.ident
                if ident is None:
                    continue
                is_main = ident == self.main_tid
                if not is_main and not self.states.get(ident):
                    continue
                out.append((self.dap_thread_id(ident), ident, is_main))
        out.sort(key=lambda x: (not x[2], x[0]))
        return out

    def thread_name(self, ident, is_main):
        if is_main:
            return "main"
        with self.lock:
            return self.thread_names.get(ident, "task")


def to_display_str(s):
    if isinstance(s, bytes):
        return s.decode("utf-8", "replace")
    return str(s)


# ---------------------------------------------------------------------------
# console expressions — annotating what the checker never saw
# ---------------------------------------------------------------------------

def resolve_static_type(engine, e, frame):
    """Best-effort declared type of a console expression, from the
    paused frame's declarations and the program's own fn signatures.
    Returns the type string or None — never a guess."""
    k = e.get("k")
    if k == "ident":
        return engine.info.var_types(frame["fn"]).get(e["name"])
    if k == "field":
        t = resolve_static_type(engine, e["target"], frame)
        if t and _struct_name(t):
            sdef = engine.info.program["structs"].get(t)
            if sdef:
                for (fname, fty, _d) in sdef["fields"]:
                    if fname == e["name"]:
                        return fty
        return None
    if k == "index":
        t = resolve_static_type(engine, e["target"], frame)
        if t and t.startswith("list["):
            return t[5:-1]
        return None
    if k == "call" and isinstance(e.get("rc"), tuple) \
            and e["rc"][0] == "user":
        fn = engine.info.program["fns"].get(e["rc"][1])
        if fn is not None and fn.get("ret") not in (None, "void"):
            return fn["ret"]
    if k == "structlit":
        return e.get("name")
    if k == "enumlit":
        return e.get("enum_name")
    return None


def annotate_console_expr(engine, e, env, frame, depth=0):
    """Fill in the dispatch annotations the checker adds to compiled
    expressions (rc on calls, rm on methods) so the interpreter can run
    a console expression against the paused frame. Resolution follows
    the checker's own rules: user fns first (redefinition is a compile
    error elsewhere, so the order never conflicts), enum constructors
    only when the base name is an enum TYPE and not a binding, methods
    through the declared type when it is visible and as builtin
    methods otherwise. Returns an error string or None."""
    if depth > 64:
        return "expression too deep"
    k = e.get("k")
    if k in ("int", "float", "bool", "str", "ident"):
        return None
    if k == "call":
        name = e.get("name")
        if name in engine.info.program["fns"]:
            e["rc"] = ("user", name)
        else:
            # builtin dispatch panics with 'unknown builtin' for a
            # name that is neither — an honest console error
            e["rc"] = ("builtin", name)
        for a in e.get("args") or []:
            err = annotate_console_expr(engine, a, env, frame, depth + 1)
            if err:
                return err
        return None
    if k == "fieldcall":
        tgt = e["target"]
        name = e["name"]
        ename = None
        if tgt.get("k") == "ident":
            n = tgt["name"]
            bound = any(n in scope for scope in env)
            if n in engine.info.program["enums"] and not bound:
                ename = n
        if ename is not None:
            args = e["args"]
            e.clear()
            e.update({"k": "enumlit", "enum_name": ename,
                      "variant": name, "args": args})
            for a in args:
                err = annotate_console_expr(engine, a, env, frame,
                                            depth + 1)
                if err:
                    return err
            return None
        ty = resolve_static_type(engine, tgt, frame)
        mkey = "%s.%s" % (ty, name) if ty and _struct_name(ty) else None
        if mkey and mkey in engine.info.program["fns"]:
            e["rm"] = ("user", mkey)
        else:
            e["rm"] = ("builtin", name)
        err = annotate_console_expr(engine, tgt, env, frame, depth + 1)
        if err:
            return err
        for a in e.get("args") or []:
            err = annotate_console_expr(engine, a, env, frame, depth + 1)
            if err:
                return err
        return None
    if k == "method":
        # a checker-shaped node cannot occur in fresh text, but be
        # tolerant if one ever arrives
        return None
    if k == "field":
        return annotate_console_expr(engine, e["target"], env, frame,
                                     depth + 1)
    if k == "index":
        err = annotate_console_expr(engine, e["target"], env, frame,
                                    depth + 1)
        if err:
            return err
        return annotate_console_expr(engine, e["idx"], env, frame,
                                     depth + 1)
    if k == "un":
        return annotate_console_expr(engine, e["e"], env, frame,
                                     depth + 1)
    if k == "bin":
        err = annotate_console_expr(engine, e["l"], env, frame,
                                    depth + 1)
        if err:
            return err
        return annotate_console_expr(engine, e["r"], env, frame,
                                     depth + 1)
    if k == "listlit":
        for it in e.get("items") or []:
            err = annotate_console_expr(engine, it, env, frame,
                                        depth + 1)
            if err:
                return err
        return None
    if k == "structlit":
        for _fname, fe in e.get("fields") or []:
            err = annotate_console_expr(engine, fe, env, frame,
                                        depth + 1)
            if err:
                return err
        return None
    if k == "enumlit":
        for a in e.get("args") or []:
            err = annotate_console_expr(engine, a, env, frame,
                                        depth + 1)
            if err:
                return err
        return None
    if k == "match":
        err = annotate_console_expr(engine, e["scrut"], env, frame,
                                    depth + 1)
        if err:
            return err
        for arm in e.get("arms") or []:
            err = annotate_console_expr(engine, arm["body"], env, frame,
                                        depth + 1)
            if err:
                return err
        return None
    if k == "qmark":
        return annotate_console_expr(engine, e["e"], env, frame,
                                     depth + 1)
    return None  # unknown node shapes evaluate as-is; failures are
    # reported as console errors, never crashes


# ---------------------------------------------------------------------------
# variables expansion — children from the real declarations
# ---------------------------------------------------------------------------

def struct_children(engine, value, ty):
    """Rows for a struct value: declared fields in declaration order."""
    sname = _struct_name(ty) or _match_struct(engine, value)
    sdef = engine.info.program["structs"].get(sname) if sname else None
    rows = []
    if sdef is not None:
        decl = {name: fty for (name, fty, _d) in sdef["fields"]}
        for fname in value.keys():
            fty = decl.get(fname)
            rows.append(make_var(engine, fname, value[fname], fty))
    else:
        for k in value.keys():
            rows.append(make_var(engine, str(k), value[k], None))
    return rows


def _match_struct(engine, value):
    """The declared type is unknown: match the field-name SET against
    the program's structs. Only an EXACT unique match counts — two
    candidates (or zero) mean 'map', never a guess."""
    if not isinstance(value, dict):
        return None
    keys = set(value.keys())
    hit = None
    for sname, sdef in engine.info.program["structs"].items():
        if {f[0] for f in sdef["fields"]} == keys:
            if hit is not None:
                return None
            hit = sname
    return hit


def enum_payload_types(engine, ename, vname):
    edef = engine.info.program["enums"].get(ename)
    if edef is None:
        return []
    for (vn, payloads) in edef.get("variants", []):
        if vn == vname:
            return payloads
    return []


def make_var(engine, name, value, ty):
    """One variables-view row: summary, annotation, and a children
    handle when the value expands."""
    ref = 0
    vt = display_type(value, ty)
    if isinstance(value, list) and value:
        elem = ty[5:-1] if ty and ty.startswith("list[") else None
        ref = engine.register_var({"kind": "list", "value": value,
                                   "elem": elem})
    elif isinstance(value, dict):
        if value.get("enum") is not None and "var" in value:
            ref = engine.register_var({"kind": "enum", "value": value})
        elif value.get("tainted"):
            ref = engine.register_var({"kind": "tainted", "value": value})
        else:
            ref = engine.register_var({"kind": "fields", "value": value,
                                       "ty": ty})
    return {
        "name": name,
        "value": display_value(value, ty),
        "type": vt,
        "variablesReference": ref,
    }


def expand_handle(engine, handle):
    kind = handle["kind"]
    if kind == "locals":
        env = handle["env"]
        frame = handle["frame"]
        vt = engine.info.var_types(frame["fn"]) if frame else {}
        rows = []
        seen = set()
        # innermost scope first; a shadowed name reports its shadow
        for scope in reversed(env):
            for name in scope.keys():
                if name in seen:
                    continue
                seen.add(name)
                rows.append(make_var(engine, name, scope[name][0],
                                     vt.get(name)))
        return rows
    v = handle["value"]
    if kind == "list":
        elem = handle.get("elem")
        return [make_var(engine, str(i), e, elem)
                for i, e in enumerate(v)]
    if kind == "fields":
        return struct_children(engine, v, handle.get("ty"))
    if kind == "enum":
        payloads = enum_payload_types(engine, v["enum"], v["var"])
        data = v.get("data") or []
        return [make_var(engine, str(i), d,
                         payloads[i] if i < len(payloads) else None)
                for i, d in enumerate(data)]
    if kind == "tainted":
        return [make_var(engine, "value", v.get("value"),
                         handle.get("ty"))]
    return []


def locals_ref(engine, frame, env):
    return engine.register_var({"kind": "locals", "frame": frame,
                                "env": env})


# ---------------------------------------------------------------------------
# the DAP session — framing, dispatch, lifecycle
# ---------------------------------------------------------------------------

class DapOut:
    """The `out` the Interp writes program stdout to; every newline (or
    explicit flush — every stop flushes) leaves as an output event."""

    def __init__(self, session):
        self.session = session
        self.buf = bytearray()
        self.lock = threading.Lock()

    def write(self, b):
        if not b:
            return len(b) if isinstance(b, (bytes, bytearray)) else 0
        # flush OUTSIDE the lock: it re-acquires (and the protocol
        # write must never hold the buffer lock)
        needs_flush = False
        with self.lock:
            self.buf += bytes(b)
            if b"\n" in bytes(b):
                needs_flush = True
        if needs_flush:
            self.flush()
        return len(b)

    def flush(self):
        text = ""
        with self.lock:
            if self.buf:
                text = bytes(self.buf).decode("utf-8", "replace")
                self.buf = bytearray()
        # send outside the buffer lock: the protocol write has its own
        if text:
            self.session.send_event("output", {"category": "stdout",
                                               "output": text})


class DapSession:
    """The adapter: reads requests on the main thread, owns the engine,
    emits events. One Halis program per adapter process (launch then
    disconnect exits) — the simplest lifecycle a DAP client can rely
    on, and the one VS Code uses for child adapters."""

    def __init__(self):
        self.write_lock = threading.Lock()
        self.seq = 1
        self.engine = None
        self.info = None
        self.initialized = False
        self.launched = False
        self.done = False
        self.run_thread = None
        self.program_args = []
        self.stop_on_entry = False
        self.no_debug = False
        self.program_path = None
        self.interp = None
        self.stdout_out = None

    # ---- protocol primitives ------------------------------------------

    def send(self, obj):
        with self.write_lock:
            obj["seq"] = self.seq
            self.seq += 1
            body = json.dumps(obj).encode("utf-8")
            sys.stdout.buffer.write(
                b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            sys.stdout.buffer.flush()

    def respond(self, req, body=None, success=True, message=None):
        resp = {"type": "response",
                "request_seq": req.get("seq", 0),
                "success": success,
                "command": req.get("command", "")}
        if message is not None:
            resp["message"] = message
        if body is not None:
            resp["body"] = body
        if not success:
            resp.setdefault("body", {})
        self.send(resp)

    def respond_error(self, req, message):
        self.respond(req, body={}, success=False, message=message)

    def send_event(self, event, body=None):
        ev = {"type": "event", "event": event}
        if body is not None:
            ev["body"] = body
        self.send(ev)

    def flush_output(self):
        # the engine pause path: program stdout is current before the
        # stopped event lands
        if self.stdout_out is not None:
            self.stdout_out.flush()

    # ---- events ---------------------------------------------------------

    def emit_stopped(self, tid, reason):
        eng = self.engine
        if eng is None:
            return
        eng.new_generation()
        snap, _reason = eng.snapshot(tid)
        if snap is None:
            return
        eng.register_frames(tid, snap)
        body = {"reason": reason,
                "threadId": eng.dap_thread_id(tid),
                "allThreadsStopped": True}
        if reason == "breakpoint":
            body["description"] = "Breakpoint hit"
            body["text"] = "Breakpoint hit"
        elif reason == "entry":
            body["description"] = "Stopped on program entry"
            body["text"] = "Stopped on program entry"
        elif reason == "step":
            body["description"] = "Step"
        elif reason == "pause":
            body["description"] = "Paused"
        elif reason == "exception":
            p = eng.panic or {}
            body["description"] = "Halis panic"
            body["text"] = to_display_str(p.get("msg", "panic"))
        self.send_event("stopped", body)

    # ---- run thread -----------------------------------------------------

    def run_program(self):
        eng = self.engine
        eng.main_tid = threading.get_ident()
        program = self.info.program
        out = DapOut(self)
        self.stdout_out = out
        interp = Interp(program, self.program_args, out)
        self.interp = interp
        eng.install(interp)
        if self.stop_on_entry and not self.no_debug:
            eng.set_entry_stop()
        # The interpreter reports panics on sys.stderr; route those
        # bytes to the debug console too (the real stderr keeps them —
        # the adapter is transparent, never lossy).
        real_err = sys.stderr

        class Tee:
            def write(self_i, b):
                text = b.decode("utf-8", "replace") if isinstance(b, bytes) \
                    else str(b)
                self.send_event("output", {"category": "stderr",
                                           "output": text})
                return real_err.write(b)

            def flush(self_i):
                return real_err.flush()

        sys.stderr = Tee()
        try:
            code = interp.run()
        except HLPanic:
            code = 101
        finally:
            sys.stderr = real_err
            out.flush()
        eng.exit_code = code
        if (code == 101 and eng.panic is not None
                and eng.stop_on_panic and not self.no_debug):
            # park dead at the panic site: the stack is real, the
            # message is real, and continue ends the session
            p = eng.panic
            self.send_event("output", {
                "category": "stderr",
                "output": "panic: %s (at line %d)\n"
                          % (to_display_str(p["msg"]), p["line"])})
            tid = threading.get_ident()
            with eng.lock:
                st = eng._state(tid)
                st["mode"] = "paused"
                st["snap"] = p["frames"]
                st["reason"] = "exception"
                st["cmd"] = None
            self.emit_stopped(tid, "exception")
            eng.wait_panic_resume(tid)
        self.send_event("exited", {"exitCode": code})
        self.send_event("terminated")

    # ---- request handlers ------------------------------------------------

    def on_initialize(self, req):
        self.initialized = True
        self.respond(req, {
            "supportsConfigurationDoneRequest": True,
            "supportsEvaluateForHovers": True,
            "supportsTerminateRequest": True,
            "supportTerminateDebuggee": True,
            "supportSuspendDebuggee": True,
            "exceptionBreakpointFilters": [{
                "filter": "panics",
                "label": "Uncaught Halis panics",
                "description": "Break when the program panics",
                "default": True,
            }],
        })
        self.send_event("initialized")

    def on_launch(self, req):
        if not self.initialized:
            return self.respond_error(req, "not initialized")
        a = req.get("arguments") or {}
        program = a.get("program")
        if not program:
            return self.respond_error(req, "launch requires 'program'")
        if not os.path.isfile(program):
            return self.respond_error(req, "program not found: %s" % program)
        args = a.get("args") or []
        self.program_args = [str(x).encode("utf-8") for x in args]
        self.stop_on_entry = bool(a.get("stopOnEntry", False))
        self.no_debug = bool(a.get("noDebug", False))
        self.program_path = program
        # parse + check NOW: a program the compiler rejects never
        # starts debugging (the real diagnostic, boot.py's text)
        try:
            program_ast = load_program(program)
            check(program_ast)
        except HLError as ex:
            return self.respond_error(req, str(ex))
        except Exception as ex:  # defensive: never die on launch
            return self.respond_error(req, "launch failed: %s" % ex)
        self.info = ProgramInfo(program_ast, program)
        self.engine = DebugEngine(self, self.info, no_debug=self.no_debug)
        self.launched = True
        self.respond(req, {})
        self.send_event("process", {
            "name": os.path.basename(program),
            "systemProcessId": os.getpid(),
            "isLocalProcess": True,
            "startMethod": "launch",
        })

    def on_set_breakpoints(self, req):
        a = req.get("arguments") or {}
        src = (a.get("source") or {}).get("path") or ""
        eng = self.engine
        if eng is None or self.info is None:
            return self.respond_error(req, "no program loaded")
        requested = [b.get("line", 0) for b in (a.get("breakpoints") or [])]
        result = []
        verified = {}
        if src and self.info.is_program_file(src):
            valid = sorted(self.info.stmt_lines(src) or set())
            for line in requested:
                snap = None
                for v in valid:
                    if v >= line:
                        snap = v
                        break
                if snap is None:
                    result.append({"verified": False, "line": line,
                                   "message": "no statement at or after "
                                              "line %d" % line})
                else:
                    bp = {"verified": True, "line": snap}
                    if snap != line:
                        bp["message"] = "snapped to line %d (nearest " \
                                        "statement)" % snap
                    result.append(bp)
                    verified.setdefault(snap, snap)
            eng.breakpoints[src] = set(verified.keys())
        else:
            for line in requested:
                result.append({"verified": False, "line": line,
                               "message": "file is not part of the "
                                          "debugged program"})
        self.respond(req, {"breakpoints": result})

    def on_set_exception_breakpoints(self, req):
        a = req.get("arguments") or {}
        filters = a.get("filters") or []
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program loaded")
        eng.stop_on_panic = "panics" in filters
        self.respond(req, {"breakpoints": [
            {"verified": True, "filter": f} for f in filters]})

    def on_configuration_done(self, req):
        self.respond(req, {})
        if self.launched and self.run_thread is None:
            self.run_thread = threading.Thread(
                target=self.run_program, name="hls-dap-run", daemon=True)
            self.run_thread.start()

    def on_threads(self, req):
        eng = self.engine
        if eng is None:
            return self.respond(req, {"threads": []})
        threads = []
        for (t, ident, is_main) in eng.live_threads():
            threads.append({"id": t, "name": eng.thread_name(ident, is_main)})
        self.respond(req, {"threads": threads})

    def _resolve_thread(self, req, arg_tid):
        eng = self.engine
        if eng is None:
            self.respond_error(req, "no program")
            return None
        for (t, ident, is_main) in eng.live_threads():
            if t == arg_tid:
                return ident
        self.respond_error(req, "unknown threadId %s" % arg_tid)
        return None

    def on_stack_trace(self, req):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        ident = self._resolve_thread(req, a.get("threadId", 1))
        if ident is None:
            return
        snap, reason = eng.snapshot(ident)
        if snap is None:
            return self.respond_error(req, "program is running")
        eng.register_frames(ident, snap)
        frames = []
        # DAP: stackFrames[0] is the INNERMOST frame; the interpreter's
        # frame stack grows by pushing, so the response walks it in
        # reverse (top of stack first)
        for i in range(len(snap) - 1, -1, -1):
            f = snap[i]
            file = f.get("file") or ""
            frames.append({
                "id": self._frame_id(ident, f, snap),
                "name": f.get("name") or f.get("fn") or "?",
                "source": {"name": os.path.basename(file),
                           "path": file} if file else {"name": "?"},
                "line": f.get("line") or 0,
                "column": 1,
            })
        self.respond(req, {"stackFrames": frames,
                           "totalFrames": len(frames)})

    def _frame_id(self, ident, frame, snap):
        eng = self.engine
        # frames were just registered by on_stack_trace; find the id
        for fid, (t, i) in eng.frame_handles.items():
            if t == ident and snap[i] is frame:
                return fid
        return 0

    def on_scopes(self, req):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        found = eng.frame_handles.get(a.get("frameId", 0))
        if found is None:
            return self.respond_error(req, "unknown frameId")
        tid, idx = found
        if not eng.is_paused(tid):
            return self.respond_error(req, "program is running")
        snap, _r = eng.snapshot(tid)
        if snap is None or idx >= len(snap):
            return self.respond_error(req, "stale frameId")
        frame = snap[idx]
        if frame.get("env") is None:
            return self.respond(req, {"scopes": []})
        ref = locals_ref(eng, frame, frame["env"])
        self.respond(req, {"scopes": [{
            "name": "Locals",
            "presentationHint": "locals",
            "variablesReference": ref,
            "expensive": False,
        }]})

    def on_variables(self, req):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        ref = a.get("variablesReference", 0)
        handle = eng.var_handles.get(ref)
        if handle is None:
            return self.respond_error(req, "unknown variablesReference")
        try:
            rows = expand_handle(eng, handle)
        except HLPanic as ex:
            return self.respond_error(req, "panic while reading: %s"
                                      % to_display_str(ex.msg))
        self.respond(req, {"variables": rows})

    def on_evaluate(self, req):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        expr = a.get("expression", "")
        frame_id = a.get("frameId", 0)
        found = eng.frame_handles.get(frame_id)
        if found is None:
            return self.respond_error(req, "evaluate needs a paused frame")
        tid, idx = found
        if not eng.is_paused(tid):
            return self.respond_error(req, "program is running")
        snap, _r = eng.snapshot(tid)
        if snap is None or idx >= len(snap):
            return self.respond_error(req, "stale frameId")
        frame = snap[idx]
        if frame.get("env") is None:
            return self.respond_error(req, "frame has no environment")
        try:
            toks = tokenize(expr.encode("utf-8"))
            p = Parser(toks)
            ast = p.parse_expr()
            if p.pos != len(toks) - 1:
                return self.respond_error(
                    req, "not a single expression: %s" % expr)
            err = annotate_console_expr(self.engine, ast, frame["env"],
                                        frame)
            if err:
                return self.respond_error(req, err)
        except HLError as ex:
            return self.respond_error(req, str(ex))
        except Exception as ex:
            return self.respond_error(req, "cannot evaluate: %s" % ex)
        # declared type for a bare identifier (children expand with it)
        ty = None
        if ast.get("k") == "ident":
            ty = self.info.var_types(frame["fn"]).get(ast["name"])
        eng.in_eval += 1
        try:
            value = self.interp.eval_expr(ast, frame["env"])
        except HLPanic as ex:
            return self.respond_error(
                req, "panic: %s (at line %d)"
                     % (to_display_str(ex.msg), ex.line))
        except Exception as ex:
            return self.respond_error(req, "cannot evaluate: %s" % ex)
        finally:
            eng.in_eval -= 1
        ref = 0
        if isinstance(value, (list, dict)) and value:
            if isinstance(value, list):
                elem = ty[5:-1] if ty and ty.startswith("list[") else None
                ref = eng.register_var({"kind": "list", "value": value,
                                        "elem": elem})
            elif value.get("enum") is not None and "var" in value:
                ref = eng.register_var({"kind": "enum", "value": value})
            elif value.get("tainted"):
                ref = eng.register_var({"kind": "tainted", "value": value})
            else:
                ref = eng.register_var({"kind": "fields", "value": value,
                                        "ty": ty})
        self.respond(req, {"result": display_value(value, ty),
                           "type": display_type(value, ty),
                           "variablesReference": ref})

    def on_continue(self, req):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        ident = self._resolve_thread(req, a.get("threadId", 1))
        if ident is None:
            return
        if not eng.resume_continue(ident):
            return self.respond_error(req, "program is not paused")
        self.respond(req, {"allThreadsContinued": True})

    def _on_step(self, req, verb):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        ident = self._resolve_thread(req, a.get("threadId", 1))
        if ident is None:
            return
        if not eng.resume_step(ident, verb):
            return self.respond_error(req, "program is not paused")
        self.respond(req, {})

    def on_next(self, req):
        self._on_step(req, "over")

    def on_step_in(self, req):
        self._on_step(req, "in")

    def on_step_out(self, req):
        self._on_step(req, "out")

    def on_pause(self, req):
        eng = self.engine
        if eng is None:
            return self.respond_error(req, "no program")
        a = req.get("arguments") or {}
        ident = self._resolve_thread(req, a.get("threadId", 1))
        if ident is None:
            return
        eng.request_pause(ident)
        self.respond(req, {})

    def on_disconnect(self, req):
        self.respond(req, {})
        self.done = True
        # a killed debuggee may leave daemon task threads mid-builtin;
        # after the reply is flushed the process exits, always
        os._exit(0)

    def on_terminate(self, req):
        self.respond(req, {})
        self.send_event("terminated")
        os._exit(0)

    # ---- dispatch --------------------------------------------------------

    HANDLERS = {
        "initialize": "on_initialize",
        "launch": "on_launch",
        "setBreakpoints": "on_set_breakpoints",
        "setExceptionBreakpoints": "on_set_exception_breakpoints",
        "configurationDone": "on_configuration_done",
        "threads": "on_threads",
        "stackTrace": "on_stack_trace",
        "scopes": "on_scopes",
        "variables": "on_variables",
        "evaluate": "on_evaluate",
        "continue": "on_continue",
        "next": "on_next",
        "stepIn": "on_step_in",
        "stepOut": "on_step_out",
        "pause": "on_pause",
        "disconnect": "on_disconnect",
        "terminate": "on_terminate",
    }

    def dispatch(self, msg):
        if msg.get("type") != "request":
            return  # responses/events from the client are ignored
        cmd = msg.get("command", "")
        name = self.HANDLERS.get(cmd)
        if name is None:
            return self.respond_error(msg, "unknown request: %s" % cmd)
        try:
            getattr(self, name)(msg)
        except Exception as ex:  # a handler bug must never kill the pipe
            try:
                self.respond_error(msg, "internal error: %s" % ex)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------

def read_frame(stream):
    """Read one DAP frame; return the decoded message, the string
    PARSE_ERROR for an undecodable body, or None at EOF. Raises
    BadFrame on a framing violation (garbage instead of headers)."""
    line = stream.readline()
    if not line:
        return None
    if line in (b"\r\n", b"\n"):
        return read_frame(stream)  # tolerate stray blank lines
    headers = {}
    while line not in (b"\r\n", b"\n"):
        if b":" not in line:
            raise BadFrame(line.decode("utf-8", "replace").strip())
        k, v = line.split(b":", 1)
        headers[k.strip().lower()] = v.strip()
        line = stream.readline()
        if not line:
            raise BadFrame("truncated headers")
    raw_len = headers.get(b"content-length")
    if raw_len is None:
        raise BadFrame("missing Content-Length")
    try:
        n = int(raw_len)
    except ValueError:
        raise BadFrame("bad Content-Length")
    body = b""
    while len(body) < n:
        chunk = stream.read(n - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return "PARSE_ERROR"


def main():
    session = DapSession()
    stream = sys.stdin.buffer
    while not session.done:
        try:
            msg = read_frame(stream)
        except BadFrame:
            session.send({"type": "response", "request_seq": 0,
                          "success": False, "command": "",
                          "message": "parse error: malformed frame"})
            continue
        if msg is None:
            break  # EOF: the client went away
        if msg == "PARSE_ERROR":
            session.send({"type": "response", "request_seq": 0,
                          "success": False, "command": "",
                          "message": "parse error: invalid JSON body"})
            continue
        session.dispatch(msg)
    # EOF shutdown: die immediately (daemon run threads must not block)
    os._exit(0)


if __name__ == "__main__":
    main()
