#!/usr/bin/env python3
"""hls-repl — interactive REPL for Halis (HLS).

Stage 123 (v0.141.0-alpha): the roadmap's interactive REPL with
:type / :effects / :audit.

The REPL holds ONE live session. The session is a Halis program that
grows as you type: definitions (fn / struct / enum / impl / import /
extern) join it at the top level, statements (let / assign / if /
while / for) join the session block, and bare expressions are
evaluated immediately and printed. The discipline that shaped it is
the house discipline — never guess:

  * Every input is compiled by the REAL toolchain before anything
    runs. The session source is synthesized, loaded by the same
    loader boot.py uses (so `import "std.str"` at the prompt resolves
    exactly where it resolves for a file), and checked by the real
    checker. An input the checker rejects never touches the session.
  * Evaluation runs on the Stage-0 interpreter — one live Interp per
    session, the same reference implementation boot.py runs. Each
    committed input extends that interpreter's program tables and
    executes only the new statements, so nothing is ever re-run
    behind your back: a `write_file` you typed once is not replayed
    by the next input. Call-shaped expressions (calls, methods,
    match, `?`) join the session block after they run — their effects
    are real and the session's audit must see them; value
    expressions (`1 + 1`, `a3`) are queries and never join the state.
  * Types and effects come from the checker, not from a re-
    implementation. `:type` reads the type the checker just wrote on
    the expression's AST node; `:effects` reads the effect fixpoint's
    computed set for the session block; `:audit` prints the very same
    table `boot.py --audit` prints for the session program.
  * The session block is the REPL's own `main`. Its `uses` clause is
    grown automatically from the checker's own missing-effect
    diagnostic — and ONLY the session block's: a definition of yours
    that lacks a needed effect is reported, not silently repaired.

Commands: :type :effects :audit :env :load :reset :help :quit

Usage: python3 tools/hls-repl.py     (interactive; stdin piped works too)

Known limits (Stage-0 reference REPL): evaluation may run real effects
(a panicking input can leave earlier statements of the SAME input
already applied — bindings made there are discarded, mutations of
older values are not); `:load` resolves std./core. imports from the
repository and does not chase sibling files; `asm!` cannot execute on
the interpreter, exactly as boot.py documents.
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from boot.lexer import tokenize, HLError            # noqa: E402
from boot.parser import Parser                      # noqa: E402
from boot.checker import check                      # noqa: E402
from boot.interp import (                           # noqa: E402
    Interp, HLPanic, ReturnSig, BreakSig, ContinueSig,
)
from boot.boot import load_program, print_audit     # noqa: E402

TOOL_VERSION = "0.141.0-alpha"

# Keywords whose input is a top-level definition of the session program.
ITEM_KEYWORDS = ("fn", "struct", "enum", "impl", "import", "extern")

# The probe binding name is reserved so an expression's value can never
# collide with session state (the REPL never binds it today, but the
# reservation keeps that true if the implementation ever changes).
PROBE_PREFIX = "__repl_"

MAX_EFFECT_ROUNDS = 16

# The checker's missing-effect diagnostic, anchored to the session
# block. User functions that lack effects must surface as-is — only
# `main` is ever auto-declared, because only `main` is the REPL's own.
_MISSING_MAIN_EFFECTS_RE = re.compile(
    r"^function 'main' calls '.*' which requires effect '.*' not declared "
    r"\(declared: .*?; missing: ([^)]*)\)$")

MAX_RENDER = 240
MAX_ITEMS = 6

# Expression shapes the language accepts as statements (parse_stmt).
# When a prompt expression has one of these shapes it joins the session
# block after it runs, so the session's audit reflects what ran.
CALL_SHAPED = frozenset(("call", "method", "fieldcall", "match", "qmark"))


def _out(text):
    """Write a transcript line to stdout. All REPL output goes through
    one flushed byte writer so program stdout (which the interpreter
    writes to the same stream) never interleaves out of order."""
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


class _ProgramOut:
    """The `out` the Interp writes program stdout to: every write lands
    on the terminal immediately, so a prompt can never swallow a
    println that ran before it."""

    def write(self, b):
        sys.stdout.buffer.write(bytes(b))
        sys.stdout.buffer.flush()
        return len(b)

    def flush(self):
        sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# value display — one formatter for results and :env
# ---------------------------------------------------------------------------

def _shorten(text):
    if len(text) <= MAX_RENDER:
        return text
    return text[:MAX_RENDER] + "..."


def _quote(s):
    return _shorten(json.dumps(s))


def _base_type(ty):
    """`Box[int]` -> `Box`; `int` -> `int`; None/unknown -> None."""
    if not ty or not isinstance(ty, str):
        return None
    return ty.split("[", 1)[0]


def display(v, ty=None, structs=None):
    """Render a runtime value the way Halis means it: bools as the HLS
    literals, strings decoded (they live as bytes inside the
    interpreter), structs in field order from their declared type,
    enums as `Variant(payload)`, taint visible. The declared type comes
    from the checker; when it does not name a struct the value speaks
    for itself — a dict renders as a map, never a guess."""
    if v is None:
        return "void"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return _quote(v.decode("utf-8", "replace"))
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        head = ", ".join(display(e, None, structs) for e in v[:MAX_ITEMS])
        if len(v) > MAX_ITEMS:
            head += ", ..."
        return _shorten("[%s] (len=%d)" % (head, len(v)))
    if isinstance(v, dict):
        if v.get("enum") is not None and "var" in v:
            data = v.get("data") or []
            name = "%s.%s" % (v["enum"], v["var"])
            if data:
                return _shorten("%s(%s)" % (
                    name, ", ".join(display(d, None, structs) for d in data)))
            return name
        if v.get("tainted"):
            return _shorten("tainted[%s]" % display(v.get("value"), None, structs))
        base = _base_type(ty)
        sdef = structs.get(base) if structs else None
        if sdef is not None:
            decl = {name: fty for (name, fty, _d) in sdef["fields"]}
            items = ", ".join(
                "%s: %s" % (k, display(x, decl.get(k), structs))
                for k, x in list(v.items())[:MAX_ITEMS])
            if len(v) > MAX_ITEMS:
                items += ", ..."
            return _shorten("%s { %s }" % (base, items))
        items = ", ".join(
            "%s: %s" % (k, display(x, None, structs))
            for k, x in list(v.items())[:MAX_ITEMS])
        if len(v) > MAX_ITEMS:
            items += ", ..."
        return "{ %s }" % items if v else "{}"
    return str(v)


# ---------------------------------------------------------------------------
# input classification and completeness
# ---------------------------------------------------------------------------

def _tokens(text):
    return tokenize(text.encode("utf-8"))


def incomplete(text):
    """True when the input cannot be complete yet: a bracket that is
    still open (string literals and comments are the real lexer's
    problem, not ours), or an unterminated string literal. Anything
    else is a complete input even if it will fail to parse — the error
    is the user's answer, not an invitation to keep reading."""
    try:
        toks = _tokens(text)
    except HLError as ex:
        return "unterminated string" in ex.msg
    depth = {"{": 0, "(": 0, "[": 0}
    for t in toks:
        if t["k"] != "sym":
            continue
        v = t["v"]
        if v in depth:
            depth[v] += 1
        elif v == "}":
            depth["{"] -= 1
        elif v == ")":
            depth["("] -= 1
        elif v == "]":
            depth["["] -= 1
    return depth["{"] > 0 or depth["("] > 0 or depth["["] > 0


def classify(text):
    """Classify one (possibly multi-line) input.

    Returns one of:
      ("item",  None)       — a top-level definition (source is kept verbatim)
      ("expr",  ast)        — a single expression, freshly parsed
      ("stmts", asts)       — one or more statements, freshly parsed
      ("empty", None)       — nothing to do (blank or comment-only)
    Raises HLError when the input is neither an item, nor an expression,
    nor a statement sequence — the caller reports it verbatim.
    """
    toks = _tokens(text)
    if toks[0]["k"] == "eof":
        return ("empty", None)
    if toks[0]["k"] == "kw" and toks[0]["v"] in ITEM_KEYWORDS:
        return ("item", None)
    # Expression first: `foo(1)` is a value at the prompt, `x = 5` is a
    # statement, and the two are told apart by what survives an EOF
    # check after the expression parse.
    try:
        p = Parser(_tokens(text), text)
        e = p.parse_expr()
        if p.peek()["k"] == "eof":
            return ("expr", e)
    except HLError:
        pass
    p = Parser(_tokens(text), text)
    stmts = []
    while p.peek()["k"] != "eof":
        stmts.append(p.parse_stmt())
    return ("stmts", stmts)


# ---------------------------------------------------------------------------
# error relocation
# ---------------------------------------------------------------------------

def _lines_of(part):
    return part.count("\n") + 1


def err_text(ex, base=0):
    """Render a compile error with input-relative line numbers. `base`
    is the 1-based line where this input's own source begins inside the
    synthesized session source; line 0 (whole-program errors) and
    errors outside the input's range are shown as they are."""
    if isinstance(ex, HLError) and base and ex.line > base:
        if ex.col > 0:
            return "error: %s (line %d:%d)" % (ex.msg, ex.line - base, ex.col)
        return "error: %s (line %d)" % (ex.msg, ex.line - base)
    return "error: %s" % ex


def _remap_lines(node, offset):
    """Shift every `line` on an AST subtree by `offset`. Expression
    probes are parsed from the raw input (input-relative lines) but
    evaluated in the synthesized session source's line space; the probe
    is remapped into that space so runtime panics land in ONE frame of
    reference the formatter understands."""
    if isinstance(node, dict):
        ln = node.get("line")
        if isinstance(ln, int) and ln > 0:
            node["line"] = ln + offset
        for v in node.values():
            _remap_lines(v, offset)
    elif isinstance(node, list):
        for v in node:
            _remap_lines(v, offset)


def _missing_main_effects(msg):
    m = _MISSING_MAIN_EFFECTS_RE.match(msg)
    if not m:
        return None
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

class Session:
    """One live REPL session: the source of the growing program, the
    accumulated declared/computed effects of its session block, and the
    one live interpreter whose program tables follow every commit."""

    def __init__(self):
        self.items = []        # top-level definition sources (in order)
        self.stmts = []        # committed session-block statement sources
        self.n_stmts = 0       # how many AST statements that amounts to
        self.effects = set()   # declared effects of the session block
        self.computed = set()  # computed effects of the committed session
        self.interp = None     # created by the first successful commit
        self.prog = None       # last committed program (for :env / display)
        self.env = [{}]        # the session scope (live bindings)

    # ---- source synthesis ----

    def _source(self, effects, extra_items=(), extra_stmts=()):
        """Build the whole session program's source. Returns (src,
        stmts_base, items_base): the line counts to SUBTRACT from a raw
        diagnostic's line so the number shown is relative to the input
        that caused it (a fragment starting on synthesized line S is
        input line 1, so the subtractable base is S - 1)."""
        parts = list(self.items)
        header = "fn main()"
        if effects:
            header += " uses " + ", ".join(sorted(effects))
        parts.append(header + " {")
        stmts_base = sum(_lines_of(p) for p in parts)
        parts.extend(self.stmts)
        parts.extend(extra_stmts)
        parts.append("}")
        items_base = sum(_lines_of(p) for p in parts)
        parts.extend(extra_items)
        return "\n".join(parts), stmts_base, items_base

    def _locate_line(self, line):
        """Map a line number in the synthesized session source back to
        the material the user can see: a definition fragment (returns
        the fragment-relative line), a committed statement input (the
        input-relative line), or None when the line belongs to something
        with honest numbers of its own (an imported std/core module
        keeps its real file lines)."""
        start = 1
        for fragment in self.items:
            span = _lines_of(fragment)
            if start <= line < start + span:
                return line - start + 1
            start += span
        start += 1  # the session block's own header line
        for source in self.stmts:
            span = _lines_of(source)
            if start <= line < start + span:
                return line - start + 1
            start += span
        return None

    def _fmt_panic(self, ex, pending_base=0, pending_span=0):
        """Render a runtime panic with the most honest line number
        available: input-relative when it fell inside the input that is
        running, definition-relative when it fell inside committed
        material, and the module's own line when it came from an
        import."""
        msg = ex.msg if isinstance(ex.msg, str) \
            else ex.msg.decode("utf-8", "replace")
        if pending_base and pending_base < ex.line <= pending_base + pending_span:
            shown = ex.line - pending_base
        else:
            shown = self._locate_line(ex.line)
        if shown is None:
            return "panic: %s (at line %d)" % (msg, ex.line)
        return "panic: %s (at line %d)" % (msg, shown)

    def _load(self, src):
        """Load the synthesized source through the production loader (a
        session scratch file — the same path a real program takes, so
        imports resolve exactly where they resolve for a file). The
        scratch file is deleted the moment it is loaded."""
        fd, path = tempfile.mkstemp(prefix="hls-repl-", suffix=".hls")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(src.encode("utf-8"))
            return load_program(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _compile(self, extra_items=(), extra_stmts=(), probe=None,
                 relocate_stmts=0, relocate_items=0):
        """Synthesize, load and check the session program; auto-declare
        the session block's missing effects (from the checker's own
        diagnostic, and only for `main`). Returns
        (program, checker, effects, error) — error is None on success.
        The probe expression, when given, joins the session block as
        its last statement so the checker annotates it in session
        context; `relocate_*` are the line bases for honest errors."""
        eff = set(self.effects)
        for _ in range(MAX_EFFECT_ROUNDS):
            src, _sb, _ib = self._source(eff, extra_items, extra_stmts)
            try:
                prog = self._load(src)
            except (HLError, SystemExit, OSError) as ex:
                base = relocate_stmts if relocate_stmts else relocate_items
                return None, None, eff, err_text(ex, base)
            if probe is not None:
                prog["fns"]["main"]["body"].append(
                    {"k": "expr", "e": probe, "line": probe.get("line", 0)})
            try:
                checker = check(prog)
            except HLError as ex:
                miss = _missing_main_effects(ex.msg)
                if miss:
                    eff |= miss
                    continue
                base = relocate_stmts if relocate_stmts else relocate_items
                return None, None, eff, err_text(ex, base)
            return prog, checker, eff, None
        return None, None, eff, (
            "error: effect inference did not converge after %d rounds"
            % MAX_EFFECT_ROUNDS)

    def _install(self, prog):
        """Point the live interpreter at the freshly checked program.
        The Interp's own tables are reassigned, never mutated in place:
        definitions are immutable once committed, so nothing that ran
        before can change meaning now."""
        if self.interp is None:
            self.interp = Interp(prog, [], _ProgramOut())
        self.interp.p = prog
        self.interp.fns = prog["fns"]
        self.interp.structs = prog["structs"]
        self.interp.enums = prog.get("enums", {})
        self.prog = prog

    # ---- committing inputs ----

    def commit_item(self, fragment):
        _, _sb, ib = self._source(self.effects, extra_items=(fragment,))
        prog, checker, eff, err = self._compile(
            extra_items=(fragment,), relocate_items=ib)
        if err:
            return err
        self.items.append(fragment)
        self.effects = eff
        self.computed = set(checker.computed_effects.get("main", set()))
        self._install(prog)
        return None

    def commit_stmts(self, sources, asts):
        """Compile the input against the session, run it in a scratch
        scope, and only then commit. A runtime panic mid-input reports
        and commits nothing: bindings the input made are discarded with
        its scratch scope (mutations of OLDER values are real effects
        that already happened — the panic message says where)."""
        _, sb, _ib = self._source(self.effects, extra_stmts=sources)
        # the pending input sits AFTER the committed statements, so its
        # subtractable base is the statements region start plus the
        # committed statements' own length
        stmt_base = sb + sum(_lines_of(p) for p in self.stmts)
        pending_span = sum(_lines_of(p) for p in sources)
        prog, checker, eff, err = self._compile(
            extra_stmts=sources, relocate_stmts=stmt_base)
        if err:
            return err, False
        committed = self.prog
        self._install(prog)  # the interpreter must exist to run the input
        scratch = {}
        self.env.append(scratch)
        halted = None
        try:
            body = prog["fns"]["main"]["body"]
            for ast in body[self.n_stmts:]:
                try:
                    self.interp.exec_stmt(ast, self.env)
                except HLPanic as ex:
                    halted = self._fmt_panic(ex, stmt_base, pending_span)
                    break
                except ReturnSig:
                    halted = ("error: return is refused at the prompt "
                              "(the session block is not callable)")
                    break
                except (BreakSig, ContinueSig):
                    halted = ("error: break/continue escaped the session "
                              "block (the checker should have caught this)")
                    break
                except SystemExit as ex:
                    halted = ("error: exit(%d) is refused at the prompt "
                              "(the session is not a process; use :quit)"
                              % (int(ex.code or 0) & 0xFF))
                    break
                except KeyboardInterrupt:
                    halted = ("^C - input aborted here (earlier statements "
                              "of it already ran)")
                    break
        finally:
            self.env.pop()
        if halted:
            if committed is not None:
                self._install(committed)  # tables stay at committed state
            return halted, True
        self.env[0].update(scratch)
        self.stmts.extend(sources)
        self.n_stmts += len(asts)
        self.effects = eff
        self.computed = set(checker.computed_effects.get("main", set()))
        return None, False

    def eval_expr_input(self, ast, text, call_shaped):
        """Evaluate a bare expression in session context and print its
        value. Call-shaped expressions (calls, methods, match, `?`) join
        the session block after they run — their effects are real and
        the session's audit must see them; value expressions (`1 + 1`,
        `a3`) are queries and never join the state."""
        prog, checker, eff, err = self._compile(probe=ast)
        if err:
            return err
        # The probe's AST lines live in input space; remap them into the
        # session source's line space (right after the committed
        # statements, where the probe logically sits) so panics from the
        # expression AND panics from the functions it calls report
        # against one frame of reference.
        _src, sb, _ib = self._source(self.effects)
        pending_base = sb + sum(_lines_of(p) for p in self.stmts)
        span = _lines_of(text)
        _remap_lines(ast, pending_base)
        # reinstall: the interpreter's ASTs must carry the CURRENT
        # layout's line numbers (a definition parsed right after its
        # commit sits after main; the next synthesis moves it before —
        # its line numbers shift, and honest panics follow the current
        # source)
        self._install(prog)
        self.interp.line = ast.get("line", pending_base)
        try:
            v = self.interp.eval_expr(ast, self.env)
        except HLPanic as ex:
            return self._fmt_panic(ex, pending_base, span)
        except SystemExit as ex:
            return ("error: exit(%d) is refused at the prompt "
                    "(the session is not a process; use :quit)"
                    % (int(ex.code or 0) & 0xFF))
        except KeyboardInterrupt:
            return "^C - evaluation interrupted"
        t = ast.get("t")
        if t not in (None, "void", "never"):
            _out("= %s\n" % display(v, t, self.prog["structs"]
                                    if self.prog else None))
        if call_shaped:
            # the expression ran once, against the live environment;
            # committing its source keeps the checker's session view in
            # sync with what actually happened (nothing re-runs)
            self.stmts.append(text)
            self.n_stmts += 1
            self.effects = eff
            self.computed = set(checker.computed_effects.get("main", set()))
        return None

    # ---- commands ----

    def cmd_type(self, ast):
        prog, _checker, _eff, err = self._compile(probe=ast)
        if err:
            return err
        t = ast.get("t")
        _out(": %s\n" % (t if t else "unknown"))
        return None

    def cmd_effects(self, ast):
        prog, checker, _eff, err = self._compile(probe=ast)
        if err:
            return err
        computed = set(checker.computed_effects.get("main", set()))
        delta = computed - self.computed
        if not computed:
            _out("effects: (none - pure)\n")
        elif not self.computed:
            # a fresh session has nothing to be new relative to
            _out("effects: %s\n" % ", ".join(sorted(computed)))
        elif delta:
            _out("effects: %s (new: %s)\n"
                 % (", ".join(sorted(computed)), ", ".join(sorted(delta))))
        else:
            # the expression needs nothing the session has not already
            # declared — the honest answer names the session's set
            _out("effects: none beyond the session's (%s)\n"
                 % ", ".join(sorted(self.computed)))
        return None

    def cmd_audit(self):
        prog, checker, _eff, err = self._compile()
        if err:
            return err
        sys.stdout.flush()
        print_audit(prog, checker)
        sys.stdout.flush()
        return None

    def cmd_env(self):
        rows = []
        if self.prog is not None:
            body = self.prog["fns"]["main"]["body"][:self.n_stmts]
            for s in body:
                if s.get("k") != "let":
                    continue
                cell = self.env[0].get(s["name"])
                ty = s.get("t") or "?"
                if s.get("mut"):
                    ty = "mut " + ty
                if cell is None:
                    rows.append("  %s: %s" % (s["name"], ty))
                    continue
                shown = display(cell[0], s.get("t"),
                                self.prog["structs"] if self.prog else None)
                rows.append("  %s: %s = %s" % (s["name"], ty, shown))
        if rows:
            _out("session bindings:\n" + "\n".join(rows) + "\n")
        else:
            _out("session bindings: (none)\n")
        return None

    def cmd_load(self, path):
        path = path.strip().strip('"')
        if not path:
            return "error: :load expects a file path"
        try:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8")
        except OSError as ex:
            return "error: cannot read %s: %s" % (path, ex.strerror)
        try:
            prog = Parser(_tokens(text), text).parse_program()
        except (HLError, SystemExit) as ex:
            return "error: %s" % ex
        if "main" in prog["fns"]:
            return ("error: :load refuses files that define main (the "
                    "session block is the REPL's own main; factor the "
                    "reusable code into named fns and load those)")
        err = self.commit_item(text.rstrip("\n"))
        if err:
            return err
        n = len(prog["structs"]) + len(prog["enums"]) + len(prog["fns"])
        _out("loaded %s: %d item(s)\n" % (path, n))
        return None

    def cmd_reset(self):
        self.items = []
        self.stmts = []
        self.n_stmts = 0
        self.effects = set()
        self.computed = set()
        self.interp = None
        self.prog = None
        self.env = [{}]
        _out("session cleared\n")
        return None

# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

BANNER = (
    "Halis REPL v%s — Stage-0 reference interpreter (types + effects checked)\n"
    "Definitions extend the session; statements change it; expressions print.\n"
    "Type :help for commands.\n" % TOOL_VERSION
)

HELP = """\
commands:
  :type <expr>      the checker's inferred type of an expression
  :effects <expr>   the effects evaluating it requires, in this session
  :audit            effect tree of the session (same table as boot.py --audit)
  :env              session bindings (name: type = value)
  :load <file.hls>  load a module-style file (no main) into the session
  :reset            clear the session (bindings, definitions, effects)
  :help             this list
  :quit             leave the REPL (also :q)

expressions evaluate and print `= <value>` (calls also join the
session); let/assign/if/while/for join the session block;
fn/struct/enum/impl/import/extern extend the program.
The session block is the REPL's own main — its `uses` clause grows by
itself, your definitions' effects do not. Nothing is ever re-run behind
your back, so bind a risky call before you retry it.\
"""


def _main_indicator_hint(err):
    """A duplicate `main` at the prompt deserves its one-line fix."""
    if "duplicate function name: main" in err:
        return err + (" (the session block is the REPL's own main; "
                      "name your function something else, or :reset)")
    return err


def process(session, text):
    """Run one complete input through commands -> classify -> compile ->
    run."""
    if text.startswith(":"):
        run_command(session, text)
        return
    try:
        kind, payload = classify(text)
    except (HLError, SystemExit) as ex:
        _out("error: %s\n" % ex)
        return
    if kind == "empty":
        return
    if kind == "item":
        err = session.commit_item(text)
        if err:
            _out(_main_indicator_hint(err) + "\n")
        return
    if kind == "expr":
        for tok in _tokens(text):
            if tok["k"] == "ident" and tok["v"].startswith(PROBE_PREFIX):
                _out("error: '%s' is reserved by the REPL\n" % tok["v"])
                return
        call_shaped = payload.get("k") in CALL_SHAPED
        err = session.eval_expr_input(payload, text, call_shaped)
        if err:
            _out(err + "\n")
        return
    # statements
    for ast in payload:
        name = ast.get("name")
        if name and name.startswith(PROBE_PREFIX):
            _out("error: '%s' is reserved by the REPL\n" % name)
            return
    sources = _slice_statements(text)
    err, ran = session.commit_stmts(sources, payload)
    if err:
        _out(_main_indicator_hint(err) + "\n")


def run_command(session, text):
    """Dispatch a `:command` input. Unknown commands are refused by
    name — never silently ignored."""
    parts = text[1:].split(None, 1)
    name = parts[0] if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if name in ("quit", "q", "exit"):
        raise _Quit()
    if name in ("help", "h"):
        _out(HELP + "\n")
        return
    if name == "reset":
        session.cmd_reset()
        return
    if name == "env":
        session.cmd_env()
        return
    if name == "audit":
        err = session.cmd_audit()
        if err:
            _out(err + "\n")
        return
    if name == "load":
        err = session.cmd_load(rest)
        if err:
            _out(err + "\n")
        return
    if name in ("type", "effects"):
        if not rest:
            _out("error: :%s expects an expression\n" % name)
            return
        try:
            _kind, payload = classify(rest)
        except (HLError, SystemExit) as ex:
            _out("error: %s\n" % ex)
            return
        if _kind != "expr":
            _out("error: :%s expects an expression\n" % name)
            return
        for tok in _tokens(rest):
            if tok["k"] == "ident" and tok["v"].startswith(PROBE_PREFIX):
                _out("error: '%s' is reserved by the REPL\n" % tok["v"])
                return
        err = session.cmd_type(payload) if name == "type" \
            else session.cmd_effects(payload)
        if err:
            _out(err + "\n")
        return
    _out("error: unknown command ':%s' (try :help)\n" % name)


class _Quit(Exception):
    """Raised by :quit to unwind the input loop cleanly."""


def _slice_statements(text):
    """One source line (or block) per committed statement. Statements
    are committed as they were written, so :audit and later errors see
    the session exactly as it was typed."""
    return [text]


def read_input(prompt):
    """Read one line; raise EOFError/KeyboardInterrupt as they come."""
    return input(prompt)


def main():
    interactive = sys.stdin.isatty()
    session = Session()
    if interactive:
        _out(BANNER)
    while True:
        try:
            if interactive:
                sys.stdout.flush()
            line = read_input("hls> " if interactive else "")
        except EOFError:
            _out("\n" if interactive else "")
            break
        except KeyboardInterrupt:
            _out("^C\n")
            continue
        if not line.strip():
            continue
        text = line
        while incomplete(text):
            try:
                more = read_input("...> " if interactive else "")
            except EOFError:
                _out("error: unexpected end of input\n")
                return 0
            except KeyboardInterrupt:
                text = None
                break
            text = text + "\n" + more
        if text is None:
            _out("^C\n")
            continue
        try:
            process(session, text)
        except _Quit:
            break
        except KeyboardInterrupt:
            _out("^C\n")
        except Exception as ex:  # noqa: BLE001 — a REPL must survive
            _out("error: internal REPL error: %s\n" % ex)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # a closed pipe (head, pager) is a clean exit for a REPL
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
