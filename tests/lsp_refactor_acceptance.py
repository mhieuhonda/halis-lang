#!/usr/bin/env python3
"""Stage 115 acceptance gate — LSP refactor actions (rename, extract,
inline).

Run with `make lsp-refactor-acceptance` (or
`python3 tests/lsp_refactor_acceptance.py`).

Eight sections:

  1. the scope law    — a local renames exactly the occurrences its
                        declaration binds: another function's same-named
                        local, a sibling block's shadow-free twin, a
                        same-named field, all stay put; params, for
                        variables and match payload bindings rename with
                        their uses; the top-level textual contract (fn,
                        struct, enum across open documents) survives
  2. the renames      — field rename rewrites the declaration and every
                        `.name` access; method rename rewrites the impl
                        declaration and its call sites; a name that is
                        both field and method is refused; a new name
                        that is a keyword, a builtin, or already bound
                        in scope is refused, never silently applied
  3. the inlines      — a single-use binding splices its initializer
                        (parenthesised when it is more than one token)
                        and deletes its line, attributes included; a
                        reassigned binding, an assignment through a use,
                        an initializer naming a reassigned local, and a
                        call-bearing initializer with two uses are all
                        refused; a zero-use binding simply dies
  4. the extracts     — the type is PROBED, not guessed: `3 * 4` binds
                        int and only int, a struct-typed call binds the
                        struct, an existing `extracted` bumps to
                        `extracted2`, a cursor on a call extracts the
                        whole call; selections inside assignment targets,
                        inside match arms, spanning two statements, or
                        of a `panic(...)` (every candidate passes, so
                        none may ship) are refused
  5. the protocol     — codeActionProvider advertises exactly the two
                        refactor kinds, the version is pinned, garbage
                        positions answer empty without dying, repeated
                        requests agree byte for byte, and a didChange
                        invalidates the next plan
  6. the utf-16 line  — an emoji before the refactor site shifts bytes,
                        not character offsets: every emitted range lands
                        on the identifier it names
  7. the edits land   — every offered edit is applied to real text and
                        the result must parse, check, and (where the
                        fixture runs) print the same output as before
  8. the corpus       — lsp_smoke.py stays green; the stage's ok corpus
                        file runs under boot with its pinned output,
                        hlfmt holds it stable, hllint is clean, and the
                        refactor round-trip on it re-checks

Exit code 0 = all acceptance criteria met.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

LSP = os.path.join(ROOT, "tools", "hls-lsp.py")
SMOKE = os.path.join(ROOT, "tools", "lsp_smoke.py")
CORPUS = os.path.join(ROOT, "tests", "ok", "feat_stage115_lsp_refactor.hls")
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s115_", dir=os.path.join(ROOT, "tests"))


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


def session(ws_name, opens, requests, seed_files=None, pre_changes=None):
    """One server session. `opens`: [(relpath, text)] opened in order.
    `requests`: [(method, params-dict-minus-textDocument, open-index)].
    `seed_files`: [(relpath, text)] written to disk but NOT opened.
    `pre_changes`: [(request_index, open-index, new-text)] — didChange
    notifications flushed BEFORE that request, so a second request can
    observe an edited buffer.
    Returns (results-in-order, returncode, stderr, init)."""
    env = dict(os.environ)
    env.pop("HLS_PKG_DEPS", None)
    base = os.path.join(TMP, ws_name)
    if seed_files:
        for rel, text in seed_files:
            write(os.path.join(ws_name, rel), text)
    uris = ["file://" + os.path.join(base, rel) for rel, _t in opens]
    payload = frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
    for i, (rel, text) in enumerate(opens):
        payload += frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                          "params": {"textDocument": {"uri": uris[i],
                                                      "version": 1,
                                                      "text": text}}})
    pre = dict((rc_idx, (o_idx, text))
               for (rc_idx, o_idx, text) in (pre_changes or []))
    for i, (method, params, open_idx) in enumerate(requests):
        if i in pre:
            o_idx, text = pre[i]
            payload += frame({"jsonrpc": "2.0",
                              "method": "textDocument/didChange",
                              "params": {"textDocument": {"uri": uris[o_idx],
                                                          "version": 2},
                                         "contentChanges": [{"text": text}]}})
        p = dict(params)
        p["textDocument"] = {"uri": uris[open_idx]}
        payload += frame({"jsonrpc": "2.0", "id": 100 + i,
                          "method": method, "params": p})
    payload += frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    payload += frame({"jsonrpc": "2.0", "method": "exit"})
    proc = subprocess.Popen(
        [sys.executable, LSP], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate(payload, timeout=180)
    results = {}
    init = None
    for m in parse_frames(out):
        if m.get("id") == 1:
            init = m.get("result")
        if isinstance(m.get("id"), int) and m["id"] >= 100:
            if "error" in m:
                results[m["id"]] = ("ERROR", m["error"])
            else:
                results[m["id"]] = m.get("result")
    return [results.get(100 + i) for i in range(len(requests))], \
        proc.returncode, err.decode("utf-8", "replace"), init


def text_of(ws_name, rel):
    with open(os.path.join(TMP, ws_name, rel), "r") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Edit application (client side): LSP ranges are UTF-16; convert honestly.
# ---------------------------------------------------------------------------

def utf16_to_idx(line, units):
    """UTF-16 unit offset -> python string index within `line`."""
    n = 0
    i = 0
    while i < len(line) and n < units:
        cp = ord(line[i])
        n += 2 if cp >= 0x10000 else 1
        i += 1
    return i


def utf16_units(s):
    """UTF-16 units of a python string (the client-side counterpart of
    the server's _byte_col_to_utf16)."""
    return sum(2 if ord(c) >= 0x10000 else 1 for c in s)


def apply_edits(text, edits):
    """Apply LSP TextEdits (UTF-16 ranges) bottom-up; returns new text."""
    lines = text.split("\n")

    def pos_to_idx(p):
        line = lines[p["line"]]
        col = utf16_to_idx(line, p["character"])
        off = sum(len(l) + 1 for l in lines[:p["line"]])
        return off + col

    out = text
    for ed in sorted(edits,
                     key=lambda e: (e["range"]["start"]["line"],
                                    e["range"]["start"]["character"]),
                     reverse=True):
        a = pos_to_idx(ed["range"]["start"])
        b = pos_to_idx(ed["range"]["end"])
        out = out[:a] + ed["newText"] + out[b:]
    return out


def edits_for(result, uri_index=0):
    """Flatten a WorkspaceEdit into one edit list. Accepts the rename
    shape ({changes}) and the CodeAction shape ({edit: {changes}}); the
    gate drives one document per scenario."""
    if not result or not isinstance(result, dict):
        return []
    changes = result.get("changes")
    if changes is None and isinstance(result.get("edit"), dict):
        changes = result["edit"].get("changes")
    for _u, eds in (changes or {}).items():
        return eds
    return []


def code_actions(ws_name, rel, text, rng, extra_opens=None):
    opens = [(rel, text)] + (extra_opens or [])
    res, rc, err, init = session(ws_name, opens,
                                 [("textDocument/codeAction",
                                   {"range": rng}, 0)])
    return (res[0] if res else None), rc, err, init


def action_of_kind(result, kind):
    for a in result or []:
        if a.get("kind") == kind:
            return a
    return None


def line_of(text, needle):
    for i, line in enumerate(text.split("\n")):
        if needle in line:
            return i
    raise AssertionError("line_of: %r not found" % needle)


def col_of(text, line_idx, needle):
    return text.split("\n")[line_idx].index(needle)


# ---------------------------------------------------------------------------
# Section 1 — the scope law.
# ---------------------------------------------------------------------------

SIBLING_SRC = '''fn pick(kind: int) -> int uses IO {
    if kind == 1 {
        let label: str = "one"
        println(label)
    }
    if kind == 2 {
        let label: str = "two"
        println(label)
    }
    return kind
}
'''

TWO_FN_SRC = '''fn alpha(n: int) -> int {
    return n + 1
}

fn beta(n: int) -> int {
    return n * 2
}

fn main() -> int uses IO {
    println(alpha(1).to_str())
    println(beta(2).to_str())
    return 0
}
'''

FOR_SRC = '''fn total_up(limit: int) -> int {
    let mut total: int = 0
    for i: int in range(0, limit) {
        total = total + i
    }
    return total
}

fn main() -> int uses IO {
    println(total_up(3).to_str())
    return 0
}
'''

MATCH_SRC = '''enum Shape {
    Circle(int),
    Square(int)
}

fn area(sh: Shape) -> int {
    return match sh {
        Shape.Circle(r) => 3 * r * r,
        Shape.Square(s) => s * s
    }
}

fn main() -> int uses IO {
    println(area(Shape.Circle(2)).to_str())
    println(area(Shape.Square(3)).to_str())
    return 0
}
'''


def section_1():
    print("1. the scope law — a local renames only what its declaration "
          "binds; the top-level contract survives")

    # Sibling blocks: same name, different variables.
    l1 = line_of(SIBLING_SRC, 'let label: str = "one"')
    res, rc, err, _i = session(
        "s1a", [("sibling.hls", SIBLING_SRC)],
        [("textDocument/rename",
          {"position": {"line": l1, "character": 12}, "newName": "tag"}, 0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 2,
          "renaming the first block's label touches exactly two sites "
          "(got %d)" % len(eds))
    l2 = line_of(SIBLING_SRC, 'let label: str = "two"')
    check(all(e["range"]["start"]["line"] in (l1, l1 + 1)
              for e in eds),
          "the second block's same-named local is a different variable "
          "and stays untouched")
    new_text = apply_edits(SIBLING_SRC, eds)
    check("let tag: str = \"one\"" in new_text
          and "let label: str = \"two\"" in new_text,
          "applied edits rename one block and spare the other")

    # Another function's same-named param stays put.
    lq = line_of(TWO_FN_SRC, "fn alpha(n: int)")
    res, rc, err, _i = session(
        "s1b", [("two.hls", TWO_FN_SRC)],
        [("textDocument/rename",
          {"position": {"line": lq, "character": 9}, "newName": "x"}, 0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 2,
          "renaming alpha's param touches the declaration and alpha's "
          "one use (got %d)" % len(eds))
    lines = {e["range"]["start"]["line"] for e in eds}
    lb = line_of(TWO_FN_SRC, "fn beta(n: int)")
    check(lb not in lines and lb + 1 not in lines,
          "beta's same-named param never moves")

    # for-loop variable: declaration and uses rename together.
    li = line_of(FOR_SRC, "for i: int in range")
    res, rc, err, _i = session(
        "s1c", [("for.hls", FOR_SRC)],
        [("textDocument/rename",
          {"position": {"line": li, "character": 8}, "newName": "j"}, 0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 2,
          "the loop variable renames in its header and in the body's "
          "use (got %d)" % len(eds))
    new_text = apply_edits(FOR_SRC, eds)
    check("for j: int in range(0, limit)" in new_text
          and "total = total + j" in new_text,
          "the renamed loop re-checks by construction")

    # Match payload binding — the pattern token and both body uses.
    lr = line_of(MATCH_SRC, "Shape.Circle(r) =>")
    res, rc, err, _i = session(
        "s1d", [("match.hls", MATCH_SRC)],
        [("textDocument/rename",
          {"position": {"line": lr, "character": 21}, "newName": "radius"},
          0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 3,
          "the payload binding renames in the pattern and both arm-body "
          "uses (got %d)" % len(eds))
    new_text = apply_edits(MATCH_SRC, eds)
    check("Shape.Circle(radius) => 3 * radius * radius" in new_text,
          "the renamed arm reads the way the scope engine resolved it")
    # the sibling arm's binding is untouched
    check("Shape.Square(s) => s * s" in new_text,
          "the Square arm's own binding never moves")

    # Top-level fn: the Stage 14 textual contract across open documents.
    res, rc, err, _i = session(
        "s1e", [("a.hls", TWO_FN_SRC), ("b.hls", TWO_FN_SRC)],
        [("textDocument/rename",
          {"position": {"line": line_of(TWO_FN_SRC, "fn alpha"),
                        "character": 4}, "newName": "alpha2"}, 0),
         ("textDocument/rename",
          {"position": {"line": line_of(TWO_FN_SRC, "println(alpha(1)"),
                        "character": 12}, "newName": "alpha2"}, 0)])
    e0 = edits_for(res[0])
    e1 = edits_for(res[1])
    check(rc == 0 and len(e0) >= 2 and len(e1) >= 2,
          "a top-level fn renames from its declaration and from a call "
          "site alike (got %d and %d edits)" % (len(e0), len(e1)))


# ---------------------------------------------------------------------------
# Section 2 — the renames (fields, methods, refusals).
# ---------------------------------------------------------------------------

FIELD_SRC = '''struct Item {
    size: int,
    name: str
}

fn weigh(it: Item) -> int {
    return it.size * 2
}

fn main() -> int uses IO {
    let it: Item = Item { size: 3, name: "x" }
    println(it.size.to_str())
    println(it.name)
    return 0
}
'''

AMBIG_SRC = '''struct Box2 {
    size: int
}

impl Box2 {
    fn size(self: Box2) -> int {
        return self.size
    }
}

fn main() -> int uses IO {
    let b: Box2 = Box2 { size: 1 }
    println(b.size().to_str())
    return 0
}
'''


def section_2():
    print("2. the renames — fields move through the dot, methods move "
          "with their impls, ambiguity is an error")

    ld = line_of(FIELD_SRC, "    size: int,")
    res, rc, err, _i = session(
        "s2a", [("field.hls", FIELD_SRC)],
        [("textDocument/rename",
          {"position": {"line": ld, "character": 5}, "newName": "width"},
          0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 4,
          "the declaration, both `.size` accesses and the struct-literal "
          "key rename (got %d)" % len(eds))
    new_text = apply_edits(FIELD_SRC, eds)
    check("width: int," in new_text and "it.width * 2" in new_text
          and "Item { size: 3" not in new_text
          and "Item { width: 3" in new_text,
          "the struct literal's key renames with the field")
    check("it.name" in new_text and 'name: str' in new_text,
          "the sibling field never moves")

    # The same rename from an access site.
    la = line_of(FIELD_SRC, "it.size * 2")
    res, rc, err, _i = session(
        "s2b", [("field2.hls", FIELD_SRC)],
        [("textDocument/rename",
          {"position": {"line": la, "character": 14}, "newName": "width"},
          0)])
    check(rc == 0 and len(edits_for(res[0])) == 4,
          "renaming from the `.size` access site finds the same four "
          "edits")

    # Method rename from a call site: impl decl + call site.
    lc = line_of(AMBIG_SRC, "b.size().to_str()")
    box_ok = '''struct Box2 {
    size: int
}

impl Box2 {
    fn grow(self: Box2) -> int {
        return self.size
    }
}

fn main() -> int uses IO {
    let b: Box2 = Box2 { size: 1 }
    println(b.grow().to_str())
    return 0
}
'''
    lc = line_of(box_ok, "b.grow().to_str()")
    res, rc, err, _i = session(
        "s2c", [("meth.hls", box_ok)],
        [("textDocument/rename",
          {"position": {"line": lc, "character": 14}, "newName": "bump"},
          0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 2,
          "the method renames at its impl declaration and its call site "
          "(got %d)" % len(eds))
    new_text = apply_edits(box_ok, eds)
    check("fn bump(self: Box2)" in new_text and "b.bump()" in new_text,
          "the renamed method reads like any other impl method")

    # Ambiguity: `size` is both a field and a method -> refused.
    ls = line_of(AMBIG_SRC, "self.size")
    res, rc, err, _i = session(
        "s2d", [("ambig.hls", AMBIG_SRC)],
        [("textDocument/rename",
          {"position": {"line": ls, "character": 16}, "newName": "sz"}, 0)])
    check(isinstance(res[0], tuple) and res[0][0] == "ERROR"
          and res[0][1].get("code") == -32602,
          "a name that is both field and method is refused with an "
          "error, never half-renamed")

    # Refusals: keyword as newName; builtin as a new local name;
    # duplicate field.
    lw = line_of(FIELD_SRC, "fn weigh(it: Item)")
    res, rc, err, _i = session(
        "s2e", [("refuse.hls", FIELD_SRC)],
        [("textDocument/rename",
          {"position": {"line": la, "character": 14}, "newName": "let"}, 0),
         ("textDocument/rename",
          {"position": {"line": lw, "character": 9}, "newName": "print"}, 0),
         ("textDocument/rename",
          {"position": {"line": ld, "character": 5}, "newName": "name"}, 0)])
    check(isinstance(res[0], tuple) and res[0][1].get("code") == -32602,
          "newName `let` is refused (the lexer would tokenize it as a "
          "keyword)")
    check(isinstance(res[1], tuple) and res[1][1].get("code") == -32602,
          "newName `print` is refused for a local (a builtin cannot be "
          "shadowed into confusion)")
    check(isinstance(res[2], tuple) and res[2][1].get("code") == -32602,
          "newName `name` is refused (the struct already has that field "
          "— the rename would duplicate it)")


# ---------------------------------------------------------------------------
# Section 3 — the inlines.
# ---------------------------------------------------------------------------

INLINE_OK_SRC = '''fn double(n: int) -> int {
    return n * 2
}

fn main() -> int uses IO {
    let base: int = 21
    let doubled: int = double(base)
    println(doubled.to_str())
    return 0
}
'''

INLINE_PAREN_SRC = '''fn main() -> int uses IO {
    let a: int = 2
    let b: int = 3
    let sum: int = (a + b)
    println((sum * 4).to_str())
    return 0
}
'''

INLINE_MULTI_USE_SRC = '''fn double(n: int) -> int {
    return n * 2
}

fn main() -> int uses IO {
    let v: int = double(5)
    println(v.to_str())
    println((v + 1).to_str())
    return 0
}
'''

INLINE_REASSIGN_SRC = '''fn main() -> int uses IO {
    let mut acc: int = 0
    acc = acc + 1
    println(acc.to_str())
    return 0
}
'''

INLINE_ASSIGN_THROUGH_SRC = '''fn main() -> int uses IO {
    let xs: list[int] = [1, 2]
    xs[0] = 9
    println(xs[0].to_str())
    return 0
}
'''

INLINE_REASSIGNED_INIT_SRC = '''fn main() -> int uses IO {
    let mut base: int = 1
    let snapshot: int = base
    base = base + 1
    println((snapshot + base).to_str())
    return 0
}
'''

INLINE_ZERO_USE_SRC = '''fn main() -> int uses IO {
    let unused: int = 7
    println("ok")
    return 0
}
'''

INLINE_ATTR_SRC = '''fn main() -> int uses IO {
    #[stack]
    let buf: list[int] = [1, 2]
    println(buf[0].to_str())
    return 0
}
'''


def section_3():
    print("3. the inlines — one splice, one deletion, and four "
          "refusals that guard the semantics")

    # Single use, call initializer: splice parenthesised, line deleted.
    ld = line_of(INLINE_OK_SRC, "let doubled: int = double(base)")
    lu = line_of(INLINE_OK_SRC, "println(doubled.to_str())")
    res, rc, err, _i = session(
        "s3a", [("inl.hls", INLINE_OK_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lu, "character": 12},
                     "end": {"line": lu, "character": 12}}}, 0)])
    act = action_of_kind(res[0], "refactor.inline")
    check(act is not None, "the cursor on a use offers Inline variable")
    new_text = apply_edits(INLINE_OK_SRC, edits_for(act))
    check("let doubled" not in new_text,
          "the binding's line is gone — no orphan indent, no orphan let")
    check("println((double(base)).to_str())" in new_text,
          "the call initializer splices parenthesised into the use")
    check(srv_check(new_text),
          "the inlined file still checks")

    # Already-parenthesised single-token-shaped init splices bare.
    ls = line_of(INLINE_PAREN_SRC, "let sum: int = (a + b)")
    res, rc, err, _i = session(
        "s3b", [("inl2.hls", INLINE_PAREN_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": ls, "character": 8},
                     "end": {"line": ls, "character": 8}}}, 0)])
    act = action_of_kind(res[0], "refactor.inline")
    check(act is not None,
          "the cursor on the `let` keyword itself offers the inline")
    new_text = apply_edits(INLINE_PAREN_SRC, edits_for(act))
    check("println(((a + b) * 4).to_str())" in new_text,
          "a fully-parenthesised initializer splices bare — no double "
          "wrapping")

    # Multi-use with a call: refused (duplicating a call re-executes it).
    lm = line_of(INLINE_MULTI_USE_SRC, "let v: int = double(5)")
    res, rc, err, _i = session(
        "s3c", [("inl3.hls", INLINE_MULTI_USE_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lm, "character": 8},
                     "end": {"line": lm, "character": 8}}}, 0)])
    check(action_of_kind(res[0], "refactor.inline") is None,
          "a call-bearing initializer with two uses earns no inline")

    # Reassigned binding: refused.
    lr = line_of(INLINE_REASSIGN_SRC, "let mut acc: int = 0")
    res, rc, err, _i = session(
        "s3d", [("inl4.hls", INLINE_REASSIGN_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lr, "character": 12},
                     "end": {"line": lr, "character": 12}}}, 0)])
    check(action_of_kind(res[0], "refactor.inline") is None,
          "a reassigned binding earns no inline — `mut` means nothing "
          "to the splice")

    # Assignment through a use: refused.
    lx = line_of(INLINE_ASSIGN_THROUGH_SRC, "xs[0] = 9")
    res, rc, err, _i = session(
        "s3e", [("inl5.hls", INLINE_ASSIGN_THROUGH_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lx, "character": 1},
                     "end": {"line": lx, "character": 1}}}, 0)])
    check(action_of_kind(res[0], "refactor.inline") is None,
          "a use an assignment writes through earns no inline")

    # Initializer naming a reassigned local: refused.
    ln = line_of(INLINE_REASSIGNED_INIT_SRC, "let snapshot: int = base")
    res, rc, err, _i = session(
        "s3f", [("inl6.hls", INLINE_REASSIGNED_INIT_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": ln, "character": 8},
                     "end": {"line": ln, "character": 8}}}, 0)])
    check(action_of_kind(res[0], "refactor.inline") is None,
          "an initializer naming a reassigned local earns no inline — "
          "the splice could read a different value")

    # Zero uses: the binding simply dies.
    lz = line_of(INLINE_ZERO_USE_SRC, "let unused: int = 7")
    res, rc, err, _i = session(
        "s3g", [("inl7.hls", INLINE_ZERO_USE_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lz, "character": 8},
                     "end": {"line": lz, "character": 8}}}, 0)])
    act = action_of_kind(res[0], "refactor.inline")
    check(act is not None, "a zero-use binding still offers the inline")
    new_text = apply_edits(INLINE_ZERO_USE_SRC, edits_for(act))
    check("let unused" not in new_text and "println(\"ok\")" in new_text,
          "the dead binding is deleted, the rest untouched")
    check(srv_check(new_text), "the deletion result still checks")

    # #[stack] attribute line dies with its binding.
    lat = line_of(INLINE_ATTR_SRC, "#[stack]")
    res, rc, err, _i = session(
        "s3h", [("inl8.hls", INLINE_ATTR_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lat + 1, "character": 8},
                     "end": {"line": lat + 1, "character": 8}}}, 0)])
    act = action_of_kind(res[0], "refactor.inline")
    check(act is not None, "the cursor after an attribute still finds "
                           "the binding")
    new_text = apply_edits(INLINE_ATTR_SRC, edits_for(act))
    check("#[stack]" not in new_text and "let buf" not in new_text,
          "the attribute list dies with the binding — no orphan "
          "#[stack]")


def srv_check(text):
    """The gate's own mirror of the server's final gate: parse + check."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hlslsp_gate", os.path.join(ROOT, "tools", "hls-lsp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HLSServer()._text_checks(text)


# ---------------------------------------------------------------------------
# Section 4 — the extracts.
# ---------------------------------------------------------------------------

EXTRACT_INT_SRC = '''fn main() -> int uses IO {
    let area: int = 3 * 4 + 5
    println(area.to_str())
    return 0
}
'''

EXTRACT_BUMP_SRC = '''fn main() -> int uses IO {
    let extracted: int = 1
    let area: int = 3 * 4 + 5
    println((area + extracted).to_str())
    return 0
}
'''

EXTRACT_STRUCT_SRC = '''struct P2 {
    x: int
}

fn make() -> P2 {
    return P2 { x: 1 }
}

fn main() -> int uses IO {
    let p: P2 = make()
    println(p.x.to_str())
    return 0
}
'''

EXTRACT_STR_SRC = '''fn main() -> int uses IO {
    println(("lo" + "hi").to_str())
    return 0
}
'''

EXTRACT_TARGET_SRC = '''fn main() -> int uses IO {
    let mut acc: int = 0
    acc = acc + 5
    println(acc.to_str())
    return 0
}
'''

EXTRACT_ARM_SRC = '''enum Shape {
    Circle(int),
    Square(int)
}

fn area(sh: Shape) -> int {
    return match sh {
        Shape.Circle(r) => 3 * r * r,
        Shape.Square(s) => s * s
    }
}

fn main() -> int uses IO {
    println(area(Shape.Circle(2)).to_str())
    return 0
}
'''

EXTRACT_CALL_SRC = '''fn double(n: int) -> int {
    return n * 2
}

fn main() -> int uses IO {
    println(double(21).to_str())
    return 0
}
'''

EXTRACT_STRUCTLIT_SRC = '''struct P2 {
    x: int
}

fn main() -> int uses IO {
    let p: P2 = P2 {
        x: 7
    }
    println(p.x.to_str())
    return 0
}
'''

EXTRACT_PANIC_SRC = '''fn main() -> int uses IO {
    let x: int = 1
    println(x.to_str())
    return 0
}
'''


def section_4():
    print("4. the extracts — the probe decides the type; ambiguity, "
          "targets, arms and junk all refuse")

    # int expression.
    la = line_of(EXTRACT_INT_SRC, "let area: int = 3 * 4 + 5")
    res, rc, err, init = code_actions(
        "s4a", "x.hls", EXTRACT_INT_SRC,
        {"start": {"line": la, "character": 20},
         "end": {"line": la, "character": 25}})
    act = action_of_kind(res, "refactor.extract")
    check(act is not None, "`3 * 4` offers Extract variable")
    ed = (act or {}).get("edit", {}).get("changes", {})
    txt = json.dumps(ed)
    check("let extracted: int = 3 * 4" in txt,
          "the probe bound int — and only int — to the fresh binding")
    new_text = apply_edits(EXTRACT_INT_SRC, edits_for(act))
    check(new_text == '''fn main() -> int uses IO {
    let extracted: int = 3 * 4
    let area: int = extracted + 5
    println(area.to_str())
    return 0
}
''', "the extract lands as a line above the statement, with its indent")

    # Existing `extracted` bumps to `extracted2`.
    lb = line_of(EXTRACT_BUMP_SRC, "let area: int = 3 * 4 + 5")
    res, _rc, _err, _i = code_actions(
        "s4b", "x.hls", EXTRACT_BUMP_SRC,
        {"start": {"line": lb, "character": 20},
         "end": {"line": lb, "character": 25}})
    act = action_of_kind(res, "refactor.extract")
    txt = json.dumps((act or {}).get("edit", {}))
    check("let extracted2: int = 3 * 4" in txt,
          "a taken name bumps to extracted2 instead of colliding")

    # Struct-typed call: the probe binds the struct, not int.
    lc = line_of(EXTRACT_STRUCT_SRC, "let p: P2 = make()")
    ld = line_of(EXTRACT_STRUCT_SRC, "println(p.x.to_str())")
    res, _rc, _err, _i = code_actions(
        "s4c", "x.hls", EXTRACT_STRUCT_SRC,
        {"start": {"line": ld, "character": 12},
         "end": {"line": ld, "character": 13}})
    act = action_of_kind(res, "refactor.extract")
    txt = json.dumps((act or {}).get("edit", {}))
    check("let extracted: P2 = p" in txt,
          "a cursor on `p` probes the struct type through the binding")

    # str expression — the assertion reads the shipped edit, not its
    # JSON rendering (json.dumps would escape the quotes out from under
    # the needle).
    ls = line_of(EXTRACT_STR_SRC, 'println(("lo" + "hi").to_str())')
    res, _rc, _err, _i = code_actions(
        "s4d", "x.hls", EXTRACT_STR_SRC,
        {"start": {"line": ls, "character": 13},
         "end": {"line": ls, "character": 24}})
    act = action_of_kind(res, "refactor.extract")
    eds = edits_for(act)
    check(any(e["newText"].startswith('let extracted: str = "lo" + "hi"')
              for e in eds),
          "a string concatenation binds str — the shipped insert says "
          "so verbatim")

    # Cursor on a call identifier extracts the WHOLE call.
    lk = line_of(EXTRACT_CALL_SRC, "println(double(21).to_str())")
    res, _rc, _err, _i = code_actions(
        "s4e", "x.hls", EXTRACT_CALL_SRC,
        {"start": {"line": lk, "character": 12},
         "end": {"line": lk, "character": 12}})
    act = action_of_kind(res, "refactor.extract")
    txt = json.dumps((act or {}).get("edit", {}))
    check("let extracted: int = double(21)" in txt,
          "a cursor on `double` extends to the whole call — that is "
          "what extract-this means at a call")

    # A multi-line struct literal selection probes the struct type.
    lm = line_of(EXTRACT_STRUCTLIT_SRC, "let p: P2 = P2 {")
    res, _rc, _err, _i = code_actions(
        "s4f", "x.hls", EXTRACT_STRUCTLIT_SRC,
        {"start": {"line": lm, "character": 15},
         "end": {"line": lm + 2, "character": 5}})
    act = action_of_kind(res, "refactor.extract")
    txt = json.dumps((act or {}).get("edit", {}))
    check("let extracted: P2 = P2 {" in txt,
          "a three-line struct literal extracts whole, type P2")

    # Refusal: selection inside an assignment target.
    lt = line_of(EXTRACT_TARGET_SRC, "acc = acc + 5")
    res, _rc, _err, _i = code_actions(
        "s4g", "x.hls", EXTRACT_TARGET_SRC,
        {"start": {"line": lt, "character": 4},
         "end": {"line": lt, "character": 7}})
    check(action_of_kind(res, "refactor.extract") is None,
          "the assignment target earns no extract — the splice would "
          "write somewhere else")

    # Refusal: selection inside a match arm body.
    lar = line_of(EXTRACT_ARM_SRC, "Shape.Circle(r) =>")
    res, _rc, _err, _i = code_actions(
        "s4h", "x.hls", EXTRACT_ARM_SRC,
        {"start": {"line": lar, "character": 20},
         "end": {"line": lar, "character": 25}})
    check(action_of_kind(res, "refactor.extract") is None,
          "a match arm body has no statement position — no extract")

    # Refusal: a selection spanning two statements.
    res, _rc, _err, _i = code_actions(
        "s4i", "x.hls", EXTRACT_INT_SRC,
        {"start": {"line": la, "character": 20},
         "end": {"line": la + 1, "character": 12}})
    check(action_of_kind(res, "refactor.extract") is None,
          "a selection spanning two statements is not one expression")

    # Refusal: `panic(...)` — every candidate passes, so none may ship.
    lp = line_of(EXTRACT_PANIC_SRC, "println(x.to_str())")
    panic_src = EXTRACT_PANIC_SRC.replace(
        "println(x.to_str())", "println(panic(\"boom\").to_str())")
    lp = line_of(panic_src, "panic")
    res, _rc, _err, _i = code_actions(
        "s4j", "x.hls", panic_src,
        {"start": {"line": lp, "character": 12},
         "end": {"line": lp, "character": 24}})
    check(action_of_kind(res, "refactor.extract") is None,
          "a `panic(...)` selection passes every candidate type and "
          "therefore earns none — never guess")


# ---------------------------------------------------------------------------
# Section 5 — the protocol.
# ---------------------------------------------------------------------------

PROTO_SRC = '''fn add(a: int, b: int) -> int {
    return a + b
}

fn main() -> int uses IO {
    let total: int = add(40, 2)
    println(total.to_str())
    return 0
}
'''


def section_5():
    print("5. the protocol — capabilities, version, resilience, "
          "determinism, invalidation")

    la = line_of(PROTO_SRC, "let total: int = add(40, 2)")
    reqs = [("textDocument/codeAction",
             {"range": {"start": {"line": la, "character": 8},
                        "end": {"line": la, "character": 8}}}, 0),
            ("textDocument/codeAction",
             {"range": {"start": {"line": la, "character": 8},
                        "end": {"line": la, "character": 8}}}, 0),
            ("textDocument/codeAction",
             {"range": {"start": {"line": 400, "character": 0},
                        "end": {"line": 400, "character": 0}}}, 0),
            ("textDocument/rename",
             {"position": {"line": 400, "character": 0},
              "newName": "zz"}, 0),
            ("textDocument/codeAction",
             {"range": {"start": {"line": la, "character": 4},
                        "end": {"line": la, "character": 4}}}, 0)]
    res, rc, err, init = session("s5", [("p.hls", PROTO_SRC)], reqs)
    caps = ((init or {}).get("capabilities") or {})
    check(caps.get("codeActionProvider")
          == {"codeActionKinds": ["refactor.extract", "refactor.inline"]},
          "codeActionProvider advertises exactly the two refactor kinds")
    ver = ((init or {}).get("serverInfo") or {}).get("version")
    check(ver == "0.134.0-alpha", "serverInfo version is 0.134.0-alpha")
    check(rc == 0 and err.strip() == "",
          "the whole session runs without a server-side traceback")
    check(res[0] == res[1], "repeated codeAction requests agree byte "
                            "for byte")
    check(res[2] == [] and res[3] == {"changes": {}},
          "a garbage position answers empty (codeAction, rename) "
          "without dying")
    inline = action_of_kind(res[0], "refactor.inline")
    check(inline is not None,
          "the cursor on the binding offers the inline")
    check(inline is not None and inline.get("isPreferred") is True,
          "isPreferred marks the inline")
    check(action_of_kind(res[0], "refactor.extract") is None,
          "the binding's own name earns no extract — `let p: T = total` "
          "before the binding reads nothing new")
    # The initializer's call DOES earn the extract (cursor extends to
    # the whole call).
    res2, _rc, _err, _i = session(
        "s5c", [("p3.hls", PROTO_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": la, "character": 22},
                     "end": {"line": la, "character": 22}}}, 0)])
    extract = action_of_kind(res2[0], "refactor.extract")
    check(extract is not None
          and extract.get("isPreferred") is False,
          "the cursor on `add` offers the extract of the whole call, "
          "not preferred")

    # didChange invalidation: apply the inline, THEN ask again in the
    # same session — the plan must see the EDITED buffer, where the
    # binding is gone and no inline can be offered there.
    inline = action_of_kind(res[0], "refactor.inline")
    inlined_text = apply_edits(PROTO_SRC, edits_for(inline))
    res2, rc2, err2, _i = session(
        "s5b", [("p2.hls", PROTO_SRC)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": la, "character": 8},
                     "end": {"line": la, "character": 8}}}, 0)],
        pre_changes=[(0, 0, inlined_text)])
    check(rc2 == 0,
          "the post-edit session runs")
    check(action_of_kind(res2[0], "refactor.inline") is None,
          "after the inline was applied, the same position no longer "
          "offers it — the plan saw the edited buffer")


# ---------------------------------------------------------------------------
# Section 6 — the utf-16 line.
# ---------------------------------------------------------------------------

EMOJI_SRC = '''fn main() -> int uses IO {
    let msg: str = "\U0001F680 ready"
    println("\U0001F680 " + msg)
    return 0
}
'''


def section_6():
    print("6. the utf-16 line — the rocket shifts bytes, not character "
          "offsets")

    lm = line_of(EMOJI_SRC, "let msg")
    luse = lm + 1
    use_line = EMOJI_SRC.split("\n")[luse]
    # The use sits after the rocket: the byte prefix is longer than the
    # utf-16 prefix — that difference is the whole test.
    byte_col = len(use_line[:use_line.index("msg")].encode("utf-8"))
    char16 = utf16_units(use_line[:use_line.index("msg")])
    check(byte_col != char16,
          "the line really is byte-shifting (byte %d != utf16 %d) — "
          "the guard has something to guard" % (byte_col, char16))

    # Rename from the USE — the client sends UTF-16, the engine replies
    # in UTF-16, and both land ON the identifier.
    res, rc, err, _i = session(
        "s6", [("e.hls", EMOJI_SRC)],
        [("textDocument/rename",
          {"position": {"line": luse, "character": char16},
           "newName": "note"}, 0),
         ("textDocument/rename",
          {"position": {"line": lm, "character": 8}, "newName": "note"},
          0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 2,
          "renaming from the post-rocket use finds declaration and use "
          "(got %d)" % len(eds))
    use_eds = [e for e in eds if e["range"]["start"]["line"] == luse]
    check(bool(use_eds)
          and use_eds[0]["range"]["start"]["character"] == char16,
          "the use edit's character offset counts the rocket as TWO "
          "utf-16 units, not four bytes")
    new_text = apply_edits(EMOJI_SRC, eds)
    check("let note: str" in new_text and "+ note)" in new_text,
          "the renamed emoji file reads correctly")

    # The decl-side rename agrees.
    eds = edits_for(res[1])
    check(rc == 0 and len(eds) == 2
          and eds[0]["range"]["start"]["character"] == 8,
          "the plain-ASCII declaration edit's character offset is 8")


# ---------------------------------------------------------------------------
# Section 7 — the edits land.
# ---------------------------------------------------------------------------

def section_7():
    print("7. the edits land — every offered edit is applied and the "
          "result must check and run")

    # Inline on the corpus: `scaled` is the single-use, call-free
    # binding (`doubled` has two uses and a call-bearing initializer —
    # correctly refused).
    with open(CORPUS) as f:
        corpus = f.read()
    ld = line_of(corpus, "let scaled: int = base * 2 + 1")
    res, rc, err, _i = session(
        "s7a", [("c.hls", corpus)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": ld, "character": 8},
                     "end": {"line": ld, "character": 8}}}, 0)])
    act = action_of_kind(res[0], "refactor.inline")
    check(act is not None, "the corpus's single-use binding offers the "
                           "inline")
    new_text = apply_edits(corpus, edits_for(act))
    check(srv_check(new_text), "the inlined corpus still checks")
    out = subprocess.run(BOOT + [write("s7a_inline.hls", new_text)],
                         capture_output=True, timeout=120)
    check(out.returncode == 0 and
          out.stdout.decode().split("\n")[:2] == ["42", "43"],
          "the inlined corpus runs and prints the same first two lines")

    # Rename on the corpus: the match binding r -> radius.
    lr = line_of(corpus, "Shape.Circle(r) =>")
    res, rc, err, _i = session(
        "s7b", [("c2.hls", corpus)],
        [("textDocument/rename",
          {"position": {"line": lr, "character": 21}, "newName": "radius"},
          0)])
    eds = edits_for(res[0])
    check(rc == 0 and len(eds) == 2, "the corpus match binding renames "
                                     "at two sites")
    new_text = apply_edits(corpus, eds)
    check(srv_check(new_text), "the renamed corpus still checks")
    out = subprocess.run(BOOT + [write("s7b_rename.hls", new_text)],
                         capture_output=True, timeout=120)
    check(out.returncode == 0 and
          out.stdout.decode().split("\n")[:6] ==
          ["42", "43", "12", "24", "10", "big"],
          "the renamed corpus prints the pinned six lines")

    # Extract on the corpus: `base * 2` in the scaled line.
    lsc = line_of(corpus, "let scaled: int = base * 2 + 1")
    res, rc, err, _i = session(
        "s7c", [("c3.hls", corpus)],
        [("textDocument/codeAction",
          {"range": {"start": {"line": lsc, "character": 22},
                     "end": {"line": lsc, "character": 30}}}, 0)])
    act = action_of_kind(res[0], "refactor.extract")
    check(act is not None, "the corpus's `base * 2` offers the extract")
    new_text = apply_edits(corpus, edits_for(act))
    check(srv_check(new_text), "the extracted corpus still checks")
    out = subprocess.run(BOOT + [write("s7c_extract.hls", new_text)],
                         capture_output=True, timeout=120)
    check(out.returncode == 0 and
          out.stdout.decode().split("\n")[:6] ==
          ["42", "43", "12", "24", "10", "big"],
          "the extracted corpus prints the pinned six lines")


# ---------------------------------------------------------------------------
# Section 8 — the corpus.
# ---------------------------------------------------------------------------

def section_8():
    print("8. the corpus — smoke green, boot output pinned, fmt stable, "
          "lint clean, refactor round-trip re-checks")

    proc = subprocess.run([sys.executable, SMOKE], capture_output=True,
                          timeout=120)
    check(proc.returncode == 0,
          "lsp_smoke.py passes (protocol resilience intact)")

    with open(CORPUS) as f:
        corpus = f.read()
    proc = subprocess.run(BOOT + [CORPUS], capture_output=True, timeout=120)
    out = proc.stdout.decode("utf-8", "replace")
    check(proc.returncode == 0, "the corpus file runs under boot")
    check(out.split("\n")[:6] == ["42", "43", "12", "24", "10", "big"],
          "the corpus output is the pinned 42 / 43 / 12 / 24 / 10 / big")

    proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools",
                                                         "hlfmt.py"),
                           "-c", CORPUS], capture_output=True, timeout=60)
    check(proc.returncode == 0, "hlfmt holds the corpus stable")

    proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools",
                                                         "hllint.py"),
                           CORPUS], capture_output=True, timeout=60)
    check(proc.returncode == 0, "hllint is clean on the corpus")

    # Round-trip: rename + inline + extract all at once, then re-check.
    ld = line_of(corpus, "let doubled: int = double(base)")
    lsc = line_of(corpus, "let scaled: int = base * 2 + 1")
    res, rc, err, _i = session(
        "s8r", [("r.hls", corpus)],
        [("textDocument/rename",
          {"position": {"line": ld, "character": 8}, "newName": "twice"},
          0),
         ("textDocument/codeAction",
          {"range": {"start": {"line": ld + 1, "character": 12},
                     "end": {"line": ld + 1, "character": 12}}}, 0),
         ("textDocument/codeAction",
          {"range": {"start": {"line": lsc, "character": 24},
                     "end": {"line": lsc, "character": 32}}}, 0)])
    check(rc == 0, "the round-trip session runs")
    text = corpus
    text = apply_edits(text, edits_for(res[0]))
    act = action_of_kind(res[1], "refactor.inline")
    if act is not None:
        text = apply_edits(text, edits_for(act))
    act = action_of_kind(res[2], "refactor.extract")
    if act is not None:
        text = apply_edits(text, edits_for(act))
    check(srv_check(text), "the triple-refactored corpus still checks")
    check("let twice: int = double(base)" in text,
          "the rename landed in the round-trip text")
    out = subprocess.run(BOOT + [write("s8_roundtrip.hls", text)],
                         capture_output=True, timeout=120)
    check(out.returncode == 0 and
          out.stdout.decode().split("\n")[:6] ==
          ["42", "43", "12", "24", "10", "big"],
          "the round-tripped corpus prints the pinned six lines")


# ---------------------------------------------------------------------------

def main():
    print("Stage 115 acceptance gate — LSP refactor actions "
          "(rename, extract, inline)")
    try:
        for section in (section_1, section_2, section_3, section_4,
                        section_5, section_6, section_7, section_8):
            section()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("==========================================")
    print("GATE RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    print("==========================================")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
