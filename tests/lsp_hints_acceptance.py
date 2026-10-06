#!/usr/bin/env python3
"""Stage 114 acceptance gate — LSP inlay hints (parameter names,
binding types).

Run with `make lsp-hints-acceptance` (or
`python3 tests/lsp_hints_acceptance.py`).

Eight sections:

  1. the call sites   — a bare fn call names its arguments, a method
                        call skips the receiver, a constructor call
                        names NOTHING (variant payloads have no
                        names); a call nested inside an argument is
                        found; labels, kinds, padding, tooltips
  2. the layer law    — resolution mirrors the checker: the builtin
                        table beats a user fn of the same name, the
                        local program beats the imports, the files
                        the document imports provide what local code
                        does not define, and an OPEN buffer beats the
                        disk copy of the file it holds
  3. the guards       — arity mismatch, unknown callees, variadic
                        spawn, the builtin-method ambiguity (list.set
                        vs map.set), a method name two structs share,
                        a user method shadowing a builtin method, a
                        syntax-error document: none of them produce a
                        hint, and none of them take the server down
  4. the bindings     — match-arm payload types: checker-instantiated
                        generics print the instantiation (int, not T),
                        the wildcard `_` is skipped, a BARE variant
                        pattern is typed through the checker's
                        scrutinee resolution, and when the checker
                        never reached the arm the raw payload types
                        still land
  5. the redundancies — `f(x: x)` says nothing the source does not:
                        a bare identifier argument spelled exactly
                        like its parameter earns no hint; its
                        neighbours still do
  6. the protocol     — the capability is advertised, the version is
                        pinned, range filtering holds, output is
                        sorted, repeated requests agree, and a
                        didChange that breaks an arity drops that
                        site's hints on the next request
  7. the utf-16 line  — an emoji in a string literal before a call on
                        the same line: the hint's `character` offsets
                        are UTF-16 units, not bytes, and land ON the
                        argument
  8. the corpus       — lsp_smoke.py stays green; the stage's ok
                        corpus file runs under boot and, driven
                        through the real server, yields exactly the
                        hints its signatures and matches describe

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
CORPUS = os.path.join(ROOT, "tests", "ok", "feat_stage114_lsp_hints.hls")
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s114_", dir=os.path.join(ROOT, "tests"))


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


def session(ws_name, opens, requests, seed_files=None):
    """Run one server session. `opens`: [(relpath, text)] opened in order.
    `requests`: [(method, params-dict-minus-textDocument, open-index)].
    `seed_files`: [(relpath, text)] written to disk but NOT opened.
    Returns (results-in-order, returncode, stderr)."""
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
    for i, (method, params, open_idx) in enumerate(requests):
        p = dict(params)
        p["textDocument"] = {"uri": uris[open_idx]}
        payload += frame({"jsonrpc": "2.0", "id": 100 + i,
                          "method": method, "params": p})
    payload += frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    payload += frame({"jsonrpc": "2.0", "method": "exit"})
    proc = subprocess.Popen(
        [sys.executable, LSP], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate(payload, timeout=120)
    results = {}
    init = None
    for m in parse_frames(out):
        if m.get("id") == 1:
            init = m.get("result")
        if isinstance(m.get("id"), int) and m["id"] >= 100:
            results[m["id"]] = m.get("result")
    return [results.get(100 + i) for i in range(len(requests))], \
        proc.returncode, err.decode("utf-8", "replace"), init


def hints_of(ws_name, main_rel, main_text, extra_opens=None,
            seed_files=None, rng=None, open_idx=0):
    """One textDocument/inlayHint request; returns (hints, rc, err, init)."""
    opens = [(main_rel, main_text)] + (extra_opens or [])
    params = {}
    if rng is not None:
        params["range"] = rng
    reqs = [("textDocument/inlayHint", params, open_idx)]
    res, rc, err, init = session(ws_name, opens, reqs, seed_files)
    return (res[0] if res else []), rc, err, init


def brief(hs):
    """[(line0, label)] in response order."""
    return [(h["position"]["line"], h["label"]) for h in (hs or [])]


def find(hs, line0, label):
    """The hint at an exact (line, label) pair, or None."""
    for h in (hs or []):
        if h["position"]["line"] == line0 and h["label"] == label:
            return h
    return None


def line_of(text, needle):
    for i, line in enumerate(text.split("\n")):
        if needle in line:
            return i
    return -1


# ---------------------------------------------------------------------------

MAIN_BASIC = '''fn add(a: int, b: int) -> int {
    return a + b
}

fn shout(s: str, times: int) -> str {
    let mut out: str = s
    let mut i: int = 0
    while i < times {
        out = out + s
        i = i + 1
    }
    return out
}

struct Acc {
    total: int,
}

impl Acc {
    fn bump(self: Acc, by: int) -> Acc {
        return Acc { total: self.total + by }
    }
}

enum Shape {
    Circle(int),
    Rect(int, int),
    Point
}

fn area(sh: Shape) -> int {
    return match sh {
        Shape.Circle(r) => 3 * r * r,
        Shape.Rect(w, h) => w * h,
        Shape.Point => 0
    }
}

fn wrap(inner: int) -> int {
    return inner
}

fn main() -> int uses IO {
    let sum: int = add(40, 2)
    let banner: str = shout("hi", 2)
    let acc: Acc = Acc { total: 1 }
    let acc2: Acc = acc.bump(2)
    let a1: int = area(Shape.Circle(3))
    let a2: int = area(Shape.Rect(4, 5))
    let w: int = wrap(add(1, add(2, 3)))
    println(sum.to_str())
    return 0
}
'''


# ---------------------------------------------------------------------------
# Section 1 — the call sites.
# ---------------------------------------------------------------------------

def section_1():
    print("1. the call sites — bare fns name their arguments, methods skip "
          "the receiver, constructors name nothing, nesting is found")

    hs, rc, err, _init = hints_of("s1", "main.hls", MAIN_BASIC)
    check(rc == 0, "server exits cleanly (rc=0)")
    check(hs is not None, "inlayHint answered a list (%s)"
          % ("null" if hs is None else "%d hints" % len(hs)))
    if hs is None:
        return

    l_add = line_of(MAIN_BASIC, "add(40, 2)")
    l_shout = line_of(MAIN_BASIC, 'shout("hi", 2)')
    l_bump = line_of(MAIN_BASIC, "acc.bump(2)")
    l_circ = line_of(MAIN_BASIC, "area(Shape.Circle(3))")
    l_rect = line_of(MAIN_BASIC, "area(Shape.Rect(4, 5))")
    l_wrap = line_of(MAIN_BASIC, "wrap(add(1, add(2, 3)))")

    h = find(hs, l_add, "a:")
    check(h is not None, "bare fn: add's first argument is named a:")
    if h is not None:
        src = MAIN_BASIC.split("\n")[l_add]
        col = src.index("40")
        check(h["position"]["character"] == col,
              "bare fn: the hint sits ON the argument (character %d)" % col)
        check(h["kind"] == 2, "parameter hints carry kind=2 (Parameter)")
        check(h.get("paddingRight") is True,
              "parameter hints pad right (renders 'a: 40')")
        check("a" in (h.get("tooltip") or ""),
              "the tooltip names the parameter")
    check(find(hs, l_add, "b:") is not None,
          "bare fn: add's second argument is named b:")

    check(find(hs, l_shout, "s:") is not None,
          "bare fn: shout's first argument is named s:")
    check(find(hs, l_shout, "times:") is not None,
          "bare fn: shout's second argument is named times:")

    h = find(hs, l_bump, "by:")
    check(h is not None, "method call: the receiver is skipped, by: lands")
    check(find(hs, l_bump, "self:") is None,
          "method call: no self: hint — the receiver is not an argument")

    check(find(hs, l_circ, "sh:") is not None,
          "the outer call is hinted even when the argument is a constructor")
    check(find(hs, l_circ, "r:") is None
          and find(hs, l_circ, "radius:") is None,
          "constructor: Shape.Circle(3) names NOTHING — payloads have no "
          "parameter names")
    check(find(hs, l_rect, "sh:") is not None,
          "constructor: the Rect call's outer sh: still lands")
    check(find(hs, l_rect, "width:") is None
          and find(hs, l_rect, "height:") is None,
          "constructor: Shape.Rect(4, 5) earns no parameter hints")

    h = find(hs, l_wrap, "inner:")
    check(h is not None, "nested: wrap's argument is named inner:")
    if h is not None:
        src = MAIN_BASIC.split("\n")[l_wrap]
        col = src.index("add(1, add(2, 3))")
        check(h["position"]["character"] == col,
              "nested: inner: sits before the outer add, not the inner one")

    chars = [(h["position"]["line"], h["position"]["character"])
             for h in hs]
    check(chars == sorted(chars),
          "hints are sorted by (line, character) — editors depend on it")


# ---------------------------------------------------------------------------
# Section 2 — the layer law.
# ---------------------------------------------------------------------------

LIB_V1 = '''fn scale(base: int, factor: int) -> int {
    return base * factor
}
'''
LIB_V2 = '''fn scale(mantissa: int, exponent: int) -> int {
    return mantissa * exponent
}
'''

MAIN_IMPORT = '''import "lib.hls"

fn local_only(grit: int) -> int {
    return grit
}

fn scale(local_first: int, local_second: int) -> int {
    return local_first + local_second
}

fn main() -> int {
    let a: int = scale(6, 7)
    let b: int = local_only(1)
    return a + b
}
'''

MAIN_IMPORT_ONLY = MAIN_IMPORT.replace(
    """
fn scale(local_first: int, local_second: int) -> int {
    return local_first + local_second
}
""", "")


def section_2():
    print("2. the layer law — builtins beat user fns, local beats import, "
          "imports provide, open buffers beat disk")

    # (a) the builtin table beats a user fn of the same name — the
    # checker consults BUILTIN_FNS first, the hints mirror it.
    MAIN_SHADOW = '''fn write_file(destination: str, payload: str) -> int {
    return 0
}

fn main() -> int {
    let n: int = write_file("a.txt", "b")
    return n
}
'''
    hs, rc, err, _init = hints_of("s2a", "main.hls", MAIN_SHADOW)
    l = line_of(MAIN_SHADOW, 'write_file("a.txt", "b")')
    check(find(hs, l, "path:") is not None and find(hs, l, "content:") is not None,
          "builtin beats user fn: write_file hints use SPEC's names "
          "(path:, content:), not the shadowing signature")
    check(find(hs, l, "destination:") is None
          and find(hs, l, "payload:") is None,
          "builtin beats user fn: the shadowing names never appear")

    # (b) local beats import — both files define scale.
    hs, rc, err, _init = hints_of(
        "s2b", "main.hls", MAIN_IMPORT,
        seed_files=[("lib.hls", LIB_V1)])
    l = line_of(MAIN_IMPORT, "scale(6, 7)")
    check(find(hs, l, "local_first:") is not None
          and find(hs, l, "local_second:") is not None,
          "local beats import: the local scale's names win over lib.hls's")

    # (c) imports provide — scale exists only in the on-disk lib.
    hs, rc, err, _init = hints_of(
        "s2c", "main.hls", MAIN_IMPORT_ONLY,
        seed_files=[("lib.hls", LIB_V1)])
    l = line_of(MAIN_IMPORT_ONLY, "scale(6, 7)")
    check(find(hs, l, "base:") is not None
          and find(hs, l, "factor:") is not None,
          "imports provide: an unopened sibling's signature names the "
          "arguments (base:, factor:)")

    # (d) an OPEN buffer beats the disk copy of the file it holds.
    hs, rc, err, _init = hints_of(
        "s2d", "main.hls", MAIN_IMPORT_ONLY,
        extra_opens=[("lib.hls", LIB_V2)],
        seed_files=[("lib.hls", LIB_V1)])
    l = line_of(MAIN_IMPORT_ONLY, "scale(6, 7)")
    check(find(hs, l, "mantissa:") is not None
          and find(hs, l, "exponent:") is not None,
          "open beats disk: the OPEN lib.hls's names (mantissa:, "
          "exponent:) win over the stale on-disk copy")


# ---------------------------------------------------------------------------
# Section 3 — the guards.
# ---------------------------------------------------------------------------

def section_3():
    print("3. the guards — every ambiguity degrades to NO hint, never to a "
          "wrong one, and never takes the server down")

    # (a) arity mismatch.
    BAD_ARITY = '''fn two(a: int, b: int) -> int {
    return a + b
}

fn main() -> int {
    let x: int = two(1, 2, 3)
    return x
}
'''
    hs, rc, err, _init = hints_of("s3a", "main.hls", BAD_ARITY)
    check(not any(h["kind"] == 2 for h in (hs or [])),
          "arity mismatch: two(1, 2, 3) earns no parameter hints")

    # (b) unknown callees and the variadic shape.
    UNKNOWN = '''fn main() -> int {
    let t: int = spawn(1, 2, 3)
    return t
}
'''
    hs, rc, err, _init = hints_of("s3b", "main.hls", UNKNOWN)
    check(hs == [] or not any(h["kind"] == 2 for h in hs),
          "variadic/unresolved callee (spawn) earns no hints")

    # (c) the builtin-method ambiguity: list.set vs map.set.
    SET_AMBIG = '''fn main() -> int {
    let xs: list[int] = [1, 2]
    let m: map[str, int] = map_new()
    xs.set(0, 9)
    m.set("k", 9)
    return 0
}
'''
    hs, rc, err, _init = hints_of("s3c", "main.hls", SET_AMBIG)
    check(not any(h["label"] in ("index:", "key:", "value:")
                  for h in (hs or [])),
          "builtin-method ambiguity: .set earns no hints on either "
          "receiver (list.set(index, value) vs map.set(key, value))")

    # (d) a method name two structs share.
    TWO_STRUCTS = '''struct S1 {
    x: int,
}

struct S2 {
    x: int,
}

impl S1 {
    fn grow(self: S1, n: int) -> S1 {
        return self
    }
}

impl S2 {
    fn grow(self: S2, n: int) -> S2 {
        return self
    }
}

fn main() -> int {
    let a: S1 = S1 { x: 1 }
    let b: S2 = a.grow(2)
    return 0
}
'''
    hs, rc, err, _init = hints_of("s3d", "main.hls", TWO_STRUCTS)
    check(not any(h["kind"] == 2 for h in (hs or [])),
          "ambiguous method: grow lives on two structs — no hints "
          "(the receiver's type is not tracked at hint time)")

    # (e) a user method shadowing a builtin method name.
    SHADOW_M = '''struct S {
    v: int,
}

impl S {
    fn split(self: S, sep: str) -> S {
        return self
    }
}

fn main() -> int {
    let s: S = S { v: 1 }
    let t: S = s.split("-")
    return 0
}
'''
    hs, rc, err, _init = hints_of("s3e", "main.hls", SHADOW_M)
    check(not any(h["kind"] == 2 for h in (hs or [])),
          "shadowed builtin method: a user split() silences .split — "
          "the receiver could be either")

    # (f) a syntax-error document degrades to [] and the server lives.
    BROKEN = '''fn broken( {
    let x: int = =
}
'''
    hs, rc, err, _init = hints_of("s3f", "main.hls", BROKEN)
    check(hs == [], "syntax-error document answers [] (not an error)")
    check(rc == 0, "syntax-error document: the server still exits cleanly")

    # (g) an unknown-callee document still hints the KNOWN call.
    MIXED = '''fn known(a: int) -> int {
    return a
}

fn main() -> int {
    let x: int = known(mystery(1))
    return x
}
'''
    hs, rc, err, _init = hints_of("s3g", "main.hls", MIXED)
    l = line_of(MIXED, "known(mystery(1))")
    check(find(hs, l, "a:") is not None,
          "one unknown callee does not blind the server to the known "
          "one wrapping it")


# ---------------------------------------------------------------------------
# Section 4 — the bindings.
# ---------------------------------------------------------------------------

GENERIC_MAIN = '''enum Box[T] {
    Some(T),
    Nothing
}

fn pick(b: Box[int]) -> int {
    return match b {
        Box.Some(v) => v,
        Box.Nothing => 0
    }
}

fn main() -> int {
    let x: int = pick(Box.Some(5))
    return x - 5
}
'''

PLAIN_MAIN = '''enum Color {
    RGB(int, int, str),
    Named(str)
}

fn mix(c: Color) -> str {
    return match c {
        Color.RGB(r, g, nm) => nm,
        Color.Named(nm2) => nm2
    }
}

fn main() -> int {
    let s: str = mix(Color.RGB(1, 2, "x"))
    return s.len()
}
'''

POISON_MAIN = '''enum Color {
    RGB(int, int, str),
    Named(str)
}

fn mix(c: Color) -> str {
    let boom: int = this_fn_does_not_exist(1)
    return match c {
        Color.RGB(r, g, nm) => nm,
        Color.Named(nm2) => nm2
    }
}

fn main() -> int {
    return 0
}
'''

WILDCARD_MAIN = '''enum Pair {
    Two(int, int),
    One(int)
}

fn sum(p: Pair) -> int {
    return match p {
        Pair.Two(a, _) => a,
        Pair.One(b) => b
    }
}

fn main() -> int {
    return sum(Pair.Two(1, 2)) + sum(Pair.One(3))
}
'''


def section_4():
    print("4. the bindings — checker-instantiated generics, the wildcard "
          "skip, the bare pattern, the fallback")

    # (a) generic enum: the checker's instantiation prints `: int`.
    hs, rc, err, _init = hints_of("s4a", "main.hls", GENERIC_MAIN)
    l_v = line_of(GENERIC_MAIN, "Box.Some(v) => v")
    h = find(hs, l_v, ": int")
    check(h is not None and h["kind"] == 1,
          "generic enum: the binding shows the INSTANTIATED type (: int)")
    check(find(hs, l_v, ": T") is None,
          "generic enum: the raw type parameter never leaks through the "
          "checker path")
    if h is not None:
        src = GENERIC_MAIN.split("\n")[l_v]
        col = src.index("v") + 1
        check(h["position"]["character"] == col,
              "the type hint sits just past the binding name")

    # (b) plain enum payloads; three bindings, three types.
    hs, rc, err, _init = hints_of("s4b", "main.hls", PLAIN_MAIN)
    l_rgb = line_of(PLAIN_MAIN, "Color.RGB(r, g, nm) =>")
    check(find(hs, l_rgb, ": int") is not None,
          "plain enum: r's payload type is int")
    check(find(hs, l_rgb, ": str") is not None,
          "plain enum: nm's payload type is str")
    l_named = line_of(PLAIN_MAIN, "Color.Named(nm2) =>")
    check(find(hs, l_named, ": str") is not None,
          "second arm: nm2 is str")

    # (c) the checker never reached the arm (an unknown fn poisons
    # check before the match) — the RAW payload types still land.
    hs, rc, err, _init = hints_of("s4c", "main.hls", POISON_MAIN)
    l_rgb = line_of(POISON_MAIN, "Color.RGB(r, g, nm) =>")
    check(find(hs, l_rgb, ": int") is not None and
          find(hs, l_rgb, ": str") is not None,
          "fallback: raw variant payloads type the bindings even when "
          "the checker aborted earlier in the file")

    # (d) the wildcard `_` earns no hint; its neighbour still does.
    hs, rc, err, _init = hints_of("s4d", "main.hls", WILDCARD_MAIN)
    l_two = line_of(WILDCARD_MAIN, "Pair.Two(a, _) => a")
    hints_on_line = [h for h in (hs or [])
                     if h["position"]["line"] == l_two and h["kind"] == 1]
    check(len(hints_on_line) == 1,
          "wildcard: `_` is skipped, the real binding keeps its type "
          "(%d type hint on the arm)" % len(hints_on_line))
    # (e) a BARE variant pattern — no `Enum.` prefix: the checker
    # resolves the enum from the scrutinee and binds instantiate.
    BARE = GENERIC_MAIN.replace("Box.Some(v) => v,", "Some(v) => v,")
    hs, rc, err, _init = hints_of("s4e", "main.hls", BARE)
    l_bare = line_of(BARE, "Some(v) => v")
    check(find(hs, l_bare, ": int") is not None,
          "bare pattern: the checker's scrutinee resolution still types "
          "the binding")


# ---------------------------------------------------------------------------
# Section 5 — the redundancies.
# ---------------------------------------------------------------------------

def section_5():
    print("5. the redundancies — f(x: x) says nothing the source does not")

    RED = '''fn two(a: int, b: int) -> int {
    return a + b
}

fn main() -> int {
    let a: int = 7
    let b: int = 8
    let other: int = 9
    let x: int = two(other, a)
    let y: int = two(a, b)
    let z: int = two(other, other + 1)
    return x + y + z
}
'''
    hs, rc, err, _init = hints_of("s5", "main.hls", RED)
    l_mixed = line_of(RED, "two(other, a)")
    check(find(hs, l_mixed, "a:") is not None,
          "two(other, a): the first argument still gets a:")
    check(find(hs, l_mixed, "b:") is not None,
          "two(other, a): the second argument is a bare `a` — spelled "
          "differently from its parameter b:, so b: IS shown")
    l_pure = line_of(RED, "two(a, b)")
    check(not any(h["position"]["line"] == l_pure and h["kind"] == 2
                  for h in (hs or [])),
          "two(a, b): both arguments spell their parameter names — "
          "the whole site is silent")
    l_expr = line_of(RED, "two(other, other + 1)")
    check(find(hs, l_expr, "b:") is not None,
          "two(other, other + 1): a compound argument is never redundant")


# ---------------------------------------------------------------------------
# Section 6 — the protocol.
# ---------------------------------------------------------------------------

def section_6():
    print("6. the protocol — the capability, the version, the range, the "
          "sorted output, the didChange")

    full_range = {"start": {"line": 0, "character": 0},
                  "end": {"line": 500, "character": 0}}
    hs, rc, err, init = hints_of("s6", "main.hls", MAIN_BASIC,
                                 rng=full_range)
    caps = ((init or {}).get("capabilities") or {})
    check(caps.get("inlayHintProvider") is True,
          "initialize advertises inlayHintProvider")
    ver = ((init or {}).get("serverInfo") or {}).get("version")
    check(ver == "0.134.0-alpha", "serverInfo version is 0.134.0-alpha")

    # Range filtering: only lines 24..40.
    lo, hi = 24, 40
    hs2, _rc, _err, _i = hints_of(
        "s6b", "main.hls", MAIN_BASIC,
        rng={"start": {"line": lo, "character": 0},
             "end": {"line": hi, "character": 0}})
    check(all(lo <= h["position"]["line"] <= hi for h in (hs2 or [])),
          "range filter: every hint inside the requested lines "
          "(%d hints in %d..%d)" % (len(hs2 or []), lo, hi))
    check(len(hs2) < len(hs),
          "range filter: strictly fewer hints than the whole-document "
          "request (%d < %d)" % (len(hs2), len(hs)))

    # Stability: the same document twice in one session.
    reqs = [("textDocument/inlayHint", {}, 0),
            ("textDocument/inlayHint", {}, 0)]
    res, rc, err, _i = session("s6c", [("main.hls", MAIN_BASIC)], reqs)
    check(res[0] == res[1], "two identical requests agree byte for byte")

    # didChange: adding an argument breaks the arity — the hints for
    # that site drop on the next request.
    edits = [("textDocument/didChange",
              {"textDocument": {"uri": "PLACEHOLDER", "version": 2},
               "contentChanges": [{"text": MAIN_BASIC.replace(
                   "add(40, 2)", "add(40, 2, 3)")}]}, None)]
    base = os.path.join(TMP, "s6d")
    uri = "file://" + os.path.join(base, "main.hls")
    payload = frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
    payload += frame({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                      "params": {"textDocument": {"uri": uri, "version": 1,
                                                  "text": MAIN_BASIC}}})
    payload += frame({"jsonrpc": "2.0", "id": 100,
                      "method": "textDocument/inlayHint", "params": {
                          "textDocument": {"uri": uri}}})
    changed = MAIN_BASIC.replace("add(40, 2)", "add(40, 2, 3)")
    payload += frame({"jsonrpc": "2.0", "method": "textDocument/didChange",
                      "params": {"textDocument": {"uri": uri, "version": 2},
                                 "contentChanges": [{"text": changed}]}})
    payload += frame({"jsonrpc": "2.0", "id": 101,
                      "method": "textDocument/inlayHint", "params": {
                          "textDocument": {"uri": uri}}})
    payload += frame({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
    payload += frame({"jsonrpc": "2.0", "method": "exit"})
    proc = subprocess.Popen(
        [sys.executable, LSP], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = proc.communicate(payload, timeout=120)
    got = {}
    for m in parse_frames(out):
        if isinstance(m.get("id"), int) and m["id"] >= 100:
            got[m["id"]] = m.get("result")
    before = got.get(100) or []
    after = got.get(101) or []
    l_add = line_of(MAIN_BASIC, "add(40, 2)")
    check(find(before, l_add, "a:") is not None,
          "before the edit: add's site is hinted")
    check(find(after, l_add, "a:") is None,
          "after an arity-breaking edit: the site's hints are GONE "
          "(the didChange re-parsed, re-checked, and the guard held)")


# ---------------------------------------------------------------------------
# Section 7 — the utf-16 line.
# ---------------------------------------------------------------------------

def section_7():
    print("7. the utf-16 line — an emoji in a string literal shifts bytes, "
          "not the hint's character units")

    EMOJI = "\U0001F600"
    SRC = ('fn add(a: int, b: int) -> int {\n'
           '    return a + b\n'
           '}\n'
           '\n'
           'fn main() -> int {\n'
           '    let mood: str = "' + EMOJI + '"\n'
           '    print("' + EMOJI + '")\n'
           '    let z: int = add(mood.len(), 2)\n'
           '    return z - 3\n'
           '}\n')
    hs, rc, err, _init = hints_of("s7", "main.hls", SRC)
    l_add = line_of(SRC, "add(mood.len(), 2)")
    h = find(hs, l_add, "a:")
    check(h is not None, "the call after emoji-bearing lines is hinted")
    if h is not None:
        src = SRC.split("\n")[l_add]
        col = src.index("mood.len()")
        check(h["position"]["character"] == col,
              "character offset is UTF-16 units ON the argument (%d)" % col)
    h2 = find(hs, l_add, "b:")
    if h2 is not None:
        src = SRC.split("\n")[l_add]
        col = src.rindex("2")
        check(h2["position"]["character"] == col,
              "second argument lands at character %d" % col)

    # The decisive shape: the emoji sits in the FIRST argument, ON the
    # same line, BEFORE the hinted second argument — a byte-offset
    # implementation would overshoot the hint by 3 characters per
    # emoji (4 UTF-8 bytes, 1 UTF-16 unit) and land inside the string.
    SRC3 = ('fn join2(parts: list[str], sep: str) -> str {\n'
            '    return sep\n'
            '}\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    let s: str = join2(["' + EMOJI + '"], "b")\n'
            '    println(s)\n'
            '    return 0\n'
            '}\n')
    hs3, rc3, err3, _i = hints_of("s7b", "main.hls", SRC3)
    l_join = line_of(SRC3, 'join2(["' + EMOJI + '"], "b")')
    h3 = find(hs3, l_join, "sep:")
    check(h3 is not None, "the emoji-bearing line's second argument is hinted")
    if h3 is not None:
        src = SRC3.split("\n")[l_join]
        quote = src.index('"b"')
        col16 = len(src[:quote].encode("utf-16-le")) // 2
        check(h3["position"]["character"] == col16,
              "character offset counts the emoji as ONE UTF-16 unit "
              "(%d), not four bytes" % col16)
        check(col16 != quote,
              "the line really is byte-shifting (byte col %d != utf16 "
              "col %d) — the guard has something to guard" % (quote, col16))


# ---------------------------------------------------------------------------
# Section 8 — the corpus.
# ---------------------------------------------------------------------------

def section_8():
    print("8. the corpus — lsp_smoke.py stays green; the stage's ok file "
          "runs and yields exactly the hints it describes")

    proc = subprocess.run([sys.executable, SMOKE], capture_output=True,
                          timeout=120)
    check(proc.returncode == 0,
          "lsp_smoke.py passes (protocol resilience intact)")

    proc = subprocess.run(BOOT + [CORPUS], capture_output=True, timeout=120)
    out = proc.stdout.decode("utf-8", "replace")
    check(proc.returncode == 0, "the corpus file runs under boot")
    check(out.split("\n")[:4] == ["42", "hihihi", "47", "n=7"],
          "the corpus output is the pinned 42 / hihihi / 47 / n=7")

    with open(CORPUS, "r") as f:
        corpus_text = f.read()
    hs, rc, err, _init = hints_of("s8", "feat_stage114_lsp_hints.hls",
                                  corpus_text)
    l_add = line_of(corpus_text, "add(40, 2)")
    l_shout = line_of(corpus_text, 'shout("hi", 2)')
    l_rect_pat = line_of(corpus_text, "Shape.Rect(w, h) =>")
    l_circ_call = line_of(corpus_text, "area(Shape.Circle(3))")
    l_describe = line_of(corpus_text, "describe(t)")
    check(find(hs, l_add, "a:") is not None
          and find(hs, l_add, "b:") is not None,
          "corpus: add(40, 2) -> a:, b:")
    check(find(hs, l_shout, "s:") is not None
          and find(hs, l_shout, "times:") is not None,
          "corpus: shout(\"hi\", 2) -> s:, times:")
    check(find(hs, l_rect_pat, ": int") is not None,
          "corpus: the Rect(w, h) pattern types its bindings")
    check(find(hs, l_circ_call, "sh:") is not None
          and find(hs, l_circ_call, "r:") is None,
          "corpus: the area call is named, the Circle constructor is not")
    check(find(hs, l_describe, "t:") is None,
          "corpus: describe(t) is redundant (the argument IS t) — "
          "no hint, exactly the discipline section 5 pins")


# ---------------------------------------------------------------------------

def main():
    print("Stage 114 acceptance gate — LSP inlay hints "
          "(parameter names, binding types)")
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
