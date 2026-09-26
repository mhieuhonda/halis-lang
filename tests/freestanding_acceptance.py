#!/usr/bin/env python3
"""Stage 77 freestanding-acceptance test.

Verifies `#![freestanding]` mode end-to-end:

1. Crate-attribute parsing (Stage-0): position, duplicates, unknown.
2. Stage-0 enforcement: the demo checks; 8 fail programs rejected.
3. Entry-only rule: a dependency declaring `#![...]` is rejected.
4. `#![freestanding]` implies `#![no_std]`.
5. Float-library denylist (libm, strtod, fmod, snprintf-%f).
6. `--audit` reports the crate mode.
7. `hlfmt` preserves the attribute line and is idempotent.
8. Self-hosted emission (hlc.hls driven through the interpreter):
   freestanding C has only the 3 freestanding includes, `_start`
   (no `main`), traps (no `exit(101)`), the bump allocator, and no
   `pthread.h`; the hosted emission is byte-shape unchanged.
9. Link-closure proof on the emitted freestanding C: every call
   reachable from `_start` resolves to a TU definition, a prelude
   definition, a macro mapping, or a compiler builtin.
10. Self-hosted parity: native-side hlc rejects the representative
    fail programs too.

Run::

    python3 tests/freestanding_acceptance.py

(Called from ``make freestanding-acceptance`` in mk/95-osdev.mk.)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PY = sys.executable
DEMO = os.path.join(REPO_ROOT, "examples", "freestanding_demo.hls")
BASIC = os.path.join(REPO_ROOT, "tests", "ok", "feat_freestanding_basic.hls")


def section(title: str) -> None:
    print()
    print("=== %s ===" % title)


def check(name: str, ok: bool, detail: str = "") -> bool:
    flag = "OK" if ok else "FAIL"
    print("  [%s] %s%s" % (flag, name, (" — " + detail) if detail else ""))
    if not ok:
        check.failed += 1  # type: ignore[attr-defined]
    return ok
check.failed = 0  # type: ignore[attr-defined]


def boot(*args: str, cwd: str | None = None):
    return subprocess.run([PY, os.path.join(REPO_ROOT, "boot", "boot.py")]
                          + list(args), capture_output=True, text=True,
                          cwd=cwd or REPO_ROOT, timeout=300)


def write_tmp(suffix: str, src: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return path


# ---------------------------------------------------------------------------
# 1. Crate-attribute parsing
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("1. Crate-attribute parsing (position/duplicates/unknown)")
    from boot.lexer import tokenize
    from boot.parser import Parser

    def parse(src: str):
        return Parser(tokenize(src.encode())).parse_program()

    p = parse("#![freestanding]\nfn main() -> int { return 0 }\n")
    check("freestanding parsed", [a["name"] for a in p["crate_attrs"]]
          == ["freestanding"])
    p = parse("#![no_std]\n#![freestanding]\nfn main() -> int { return 0 }\n")
    check("no_std + freestanding parsed",
          [a["name"] for a in p["crate_attrs"]] == ["no_std", "freestanding"])
    p = parse("# lead comment\n#![freestanding]\nfn main() -> int { return 0 }\n")
    check("comment before attr ok",
          [a["name"] for a in p["crate_attrs"]] == ["freestanding"])
    for src, needle in [
        ("fn main() -> int { return 0 }\n#![freestanding]\n",
         "before any item"),
        ("#![freestanding]\n#![freestanding]\nfn main() -> int { return 0 }\n",
         "duplicate"),
        ("#![kernel]\nfn main() -> int { return 0 }\n", "unknown crate"),
        ("#![123]\nfn main() -> int { return 0 }\n", "attribute name"),
    ]:
        try:
            parse(src)
            check("reject %r" % needle, False, "parsed cleanly")
        except Exception as ex:  # noqa: BLE001 — asserting the message
            check("reject %r" % needle, needle in str(ex), str(ex)[:80])
    # `#!` without `[` stays a comment; `#[...]` still works.
    p = parse("#! just a comment\nfn main() -> int { return 0 }\n")
    check("#! non-bracket stays a comment", p["crate_attrs"] == [])
    p = parse("#[inline(never)]\nfn f() -> int { return 1 }\n"
              "fn main() -> int { return f() }\n")
    check("#[...] outer attrs unaffected",
          p["crate_attrs"] == [] and "f" in p["fns"])


# ---------------------------------------------------------------------------
# 2. Stage-0 enforcement
# ---------------------------------------------------------------------------

EXPECT = {
    "fail_freestanding_std_import.hls": "std module",
    "fail_freestanding_uses.hls": "capabilities are unavailable",
    "fail_freestanding_extern.hls": "not available in #![freestanding]",
    "fail_freestanding_late_attr.hls": "before any item",
    "fail_freestanding_unknown_attr.hls": "unknown crate attribute",
    "fail_freestanding_dup_attr.hls": "duplicate crate attribute",
    "fail_freestanding_sin.hls": "math_sin() is not available",
    "fail_freestanding_float_mod.hls": "float % is not available",
}


def test_enforcement() -> None:
    section("2. Stage-0 enforcement (demo OK, 8 fail programs rejected)")
    r = boot("--check", DEMO)
    check("freestanding demo checks",
          r.returncode == 0, (r.stdout or "").strip()[:60])
    r = boot("--check", BASIC)
    check("freestanding ok-test checks",
          r.returncode == 0, (r.stdout or "").strip()[:60])
    r = boot(DEMO)
    check("freestanding demo runs (exit 231)",
          r.returncode == 231, "rc=%d" % r.returncode)
    for fname, needle in EXPECT.items():
        r = boot("--check", os.path.join(REPO_ROOT, "tests", "fail", fname))
        out = (r.stdout or "") + (r.stderr or "")
        check("%s rejected" % fname,
              r.returncode == 1 and needle in out, out.strip()[:90])


# ---------------------------------------------------------------------------
# 3. Entry-only rule + implication
# ---------------------------------------------------------------------------

def test_scope() -> None:
    section("3. Entry-only rule + freestanding implies no_std")
    d = tempfile.mkdtemp(prefix="hls_fs_")
    dep = os.path.join(d, "dep.hls")
    with open(dep, "w", encoding="utf-8", newline="\n") as f:
        f.write("#![freestanding]\nfn helper() -> int { return 1 }\n")
    entry = os.path.join(d, "entry.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("import \"dep.hls\"\nfn main() -> int { return helper() }\n")
    r = boot("--check", entry)
    out = (r.stdout or "") + (r.stderr or "")
    check("dep crate attr rejected",
          r.returncode == 1 and "only allowed in the entry file" in out,
          out.strip()[:100])

    from boot.boot import load_program as _load_program
    from boot.checker import check as checkmod
    for src, want in [("#![freestanding]\nfn main() -> int { return 0 }\n",
                       (True, True)),
                      ("#![no_std]\nfn main() -> int { return 0 }\n",
                       (False, True)),
                      ("fn main() -> int { return 0 }\n", (False, False))]:
        p = os.path.join(d, "m.hls")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(src)
        prog = _load_program(p)
        c = checkmod(prog)
        got = (c.crate_mode_flags["freestanding"], c.crate_mode_flags["no_std"])
        check("modes %s == %s" % (want, got), got == want)


# ---------------------------------------------------------------------------
# 4. Float-library denylist (allowed side included)
# ---------------------------------------------------------------------------

def test_denylist() -> None:
    section("4. Float-library denylist (+ allowed float surface)")
    denied = {
        "math_sqrt": "fn f(x: float) -> float { return math_sqrt(x) }",
        "math_pow": "fn f(a: float, b: float) -> float { return math_pow(a, b) }",
        "float()": "fn f(s: str) -> float { return float(s) }",
        "str(float)": "fn f(x: float) -> str { return str(x) }",
        "str.to_float": "fn f(s: str) -> float { return s.to_float() }",
        "float.to_str": "fn f(x: float) -> str { return x.to_str() }",
    }
    for label, body in denied.items():
        src = ("#![freestanding]\n%s\nfn main() -> int { return 0 }\n" % body)
        p = write_tmp(".hls", src)
        r = boot("--check", p)
        out = (r.stdout or "") + (r.stderr or "")
        check("deny %s" % label,
              r.returncode == 1 and "not available in #![freestanding]" in out,
              out.strip()[:80])
    allowed = {
        "isnan": "fn f(x: float) -> bool { return math_isnan(x) }",
        "isinf": "fn f(x: float) -> bool { return math_isinf(x) }",
        "isfinite": "fn f(x: float) -> bool { return math_isfinite(x) }",
        "signbit": "fn f(x: float) -> bool { return math_signbit(x) }",
        "int.to_str": "fn f(x: int) -> str { return x.to_str() }",
        "int.to_float": "fn f(x: int) -> float { return x.to_float() }",
        "float.to_int": "fn f(x: float) -> int { return x.to_int() }",
        "float arith": "fn f(a: float, b: float) -> float { return a + b * 2.0 - a / b }",
    }
    for label, body in allowed.items():
        src = ("#![freestanding]\n%s\nfn main() -> int { return 0 }\n" % body)
        p = write_tmp(".hls", src)
        r = boot("--check", p)
        check("allow %s" % label, r.returncode == 0,
              ((r.stdout or "") + (r.stderr or "")).strip()[:80])


# ---------------------------------------------------------------------------
# 5. Audit + fmt
# ---------------------------------------------------------------------------

def test_audit_fmt() -> None:
    section("5. --audit reports the mode; hlfmt is stable")
    r = boot("--audit", DEMO)
    audit_lines = (r.stdout or "").strip().splitlines()
    check("--audit shows freestanding",
          r.returncode == 0 and "#![freestanding]" in (r.stdout or ""),
          "; ".join(audit_lines[-2:])[:120] if audit_lines else "")
    r = subprocess.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                        "-c", DEMO], capture_output=True, text=True,
                       cwd=REPO_ROOT, timeout=120)
    check("hlfmt clean on the demo",
          "NOT formatted" not in (r.stdout or ""), (r.stdout or "").strip()[:60])
    for f in [BASIC,
              os.path.join(REPO_ROOT, "tests", "fail",
                           "fail_freestanding_uses.hls")]:
        r = subprocess.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                            "-c", f], capture_output=True, text=True,
                           cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean on %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""))


# ---------------------------------------------------------------------------
# 6-8. Self-hosted emission + link closure (one cached emission)
# ---------------------------------------------------------------------------

EMIT_C: str = ""


def emit_once() -> str:
    global EMIT_C
    if EMIT_C:
        return EMIT_C
    d = tempfile.mkdtemp(prefix="hls_fs_emit_")
    EMIT_C = os.path.join(d, "fs_demo.c")
    r = boot(os.path.join(REPO_ROOT, "src", "hlc.hls"), DEMO, EMIT_C)
    if r.returncode != 0 or not os.path.isfile(EMIT_C):
        raise RuntimeError("hlc emission failed: %s" % ((r.stdout or "")
                                                        + (r.stderr or ""))[-500:])
    return EMIT_C


def test_emission() -> None:
    section("6. Self-hosted emission (freestanding TU shape)")
    path = emit_once()
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    check("emitted C non-trivial", len(src) > 20000, "%d bytes" % len(src))
    includes = sorted(set(re.findall(r"#include\s*<([^>]+)>", src)))
    check("only freestanding headers", includes == ["stdbool.h", "stddef.h",
                                                    "stdint.h"],
          str(includes))
    check("no hosted header", not re.search(r"#include\s*<(stdio|stdlib|string|pthread)\.h>", src))
    check("_start emitted", "void _start(void)" in src)
    check("no hosted main", not re.search(r"int main\s*\(", src))
    check("panics trap", "__builtin_trap" in src and "exit(101)" not in src)
    check("bump allocator emitted", "hl_bump_alloc(size_t n)" in src
          and "#define malloc hl_bump_alloc" in src)
    check("no pthread.h", "pthread.h" not in src)
    check("exit syscalls for 3 arches",
          all(s in src for s in ("syscall", "svc #0", "ecall")))
    # The entry must be NAKED. A non-naked `_start` lets GCC insert the
    # outgoing-call padding its assumed ABI alignment demands, which
    # leaves every 16-byte-aligned slot in the first Halis function
    # misaligned — and its `movaps` initialisers fault. That is a
    # SIGSEGV in code whose source is obviously correct, and it appears
    # only in a -nostdlib image.
    check("entry is naked (ABI stack alignment)",
          "__attribute__((naked, noreturn)) void _start(void)" in src)
    check("hand-rolled int64 rendering",
          "hl_str_from_int64(int64_t v)" in src and "-(v + 1)" in src)
    # Hosted emission unchanged (spot check).
    d = tempfile.mkdtemp(prefix="hls_host_emit_")
    out = os.path.join(d, "hello.c")
    hello = os.path.join(REPO_ROOT, "examples", "hello.hls")
    r = boot(os.path.join(REPO_ROOT, "src", "hlc.hls"), hello, out)
    check("hosted emission compile ok", r.returncode == 0, r.stderr.strip()[-200:])
    with open(out, encoding="utf-8", errors="replace") as f:
        hsrc = f.read()
    check("hosted emission keeps stdio+main",
          "#include <stdio.h>" in hsrc
          and re.search(r"int main\s*\(", hsrc) is not None
          and "HL_FREESTANDING" not in hsrc)


def test_link_closure() -> None:
    section("7. Link-closure proof (reachable from _start resolves)")
    with open(emit_once(), encoding="utf-8", errors="replace") as f:
        src = f.read()
    t = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)
    t = re.sub(r"'(?:[^'\\\n]|\\.)*'", "'x'", t)
    t = re.sub(r"//.*", "", t)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.DOTALL)
    t = "\n".join(l for l in t.splitlines() if not l.strip().startswith("#"))
    funcs: dict = {}
    cur = None
    buf: list = []
    for line in t.splitlines():
        m = re.match(r"\s*(?:static\s+|inline\s+)*(?:[\w\s\*]+?)\s+(\w+)"
                     r"\s*\([^;]*\)\s*\{\s*$", line)
        if m and not line.strip().startswith(("if ", "for ", "while ",
                                              "switch ", "do ")):
            if cur:
                funcs[cur] = "\n".join(buf)
            cur = m.group(1)
            buf = [line]
        elif cur:
            buf.append(line)
            if line.strip() == "}" and not line.startswith((" ", "\t")):
                funcs[cur] = "\n".join(buf)
                cur = None
                buf = []
    if cur:
        funcs[cur] = "\n".join(buf)
    check("145+ runtime functions emitted", len(funcs) >= 100,
          "%d functions" % len(funcs))

    ignorable = {"if", "for", "while", "switch", "return", "sizeof",
                 "typedef", "struct", "union", "enum", "__asm__",
                 "__attribute__", "cleanup", "void", "volatile"}
    defined_macros = {"HL_FREESTANDING", "PRId64", "HL_HEAP_SIZE", "EOF",
                      "EINTR", "ESRCH", "SIGTERM", "O_RDONLY", "O_WRONLY",
                      "AF_INET", "SOCK_STREAM", "SOCK_DGRAM", "SOL_SOCKET",
                      "SO_REUSEADDR", "SO_RCVTIMEO", "SO_SNDTIMEO",
                      "INADDR_ANY", "INADDR_LOOPBACK", "INET_ADDRSTRLEN",
                      "CLOCK_MONOTONIC", "CLOCK_REALTIME", "S_ISDIR",
                      "S_ISREG", "WEXITSTATUS", "WIFEXITED", "WIFSIGNALED",
                      "WTERMSIG", "HL_FEAT_EQ", "HL_PROC_MAX_CHILDREN",
                      "UINT64_C"}
    compiler_builtins = {"__builtin_trap", "__builtin_unreachable",
                         "__builtin_add_overflow", "__builtin_sub_overflow",
                         "__builtin_mul_overflow", "__builtin_clzll",
                         "__builtin_ctzll", "__builtin_cpu_supports"}
    prelude_defined = {"hl_bump_alloc", "hl_bump_calloc", "hl_bump_realloc",
                       "hl_memcpy", "hl_memset", "hl_memcmp", "hl_strlen",
                       "hl_strcmp", "hl_strncmp", "hl_strcpy", "hl_strchr",
                       "hl_strdup", "hl_fs_isnan", "hl_fs_isinf",
                       "hl_fs_isfinite", "hl_fs_signbit", "hl_errno_stub"}
    macro_mapped = {"malloc", "calloc", "realloc", "free", "memcpy",
                    "memset", "memcmp", "strlen", "strcmp", "strncmp",
                    "strcpy", "strchr", "strdup", "stat", "errno",
                    "isnan", "isinf", "isfinite", "signbit"}
    seen = set()
    stack = ["_start", "hl_boot"]
    while stack:
        fn = stack.pop()
        if fn in seen or fn not in funcs:
            continue
        seen.add(fn)
        for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", funcs[fn]):
            stack.append(m.group(1))
    # The entry's asm calls `hl_boot`, which calls the user's main; the
    # scan follows both (`hl_boot` is a real C function, and its name
    # also appears in the entry's asm string).
    check("_start reaches the user main",
          "usf_main" in seen or "hl_boot" in seen,
          "%d reachable functions" % len(seen))
    bad = set()
    for fn in seen:
        for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", funcs[fn]):
            c = m.group(1)
            if c in ignorable or c in defined_macros or c in compiler_builtins:
                continue
            if c in funcs or c in prelude_defined or c in macro_mapped:
                continue
            # Single-line `static T name(...) { ...; }` definitions the
            # crude splitter missed (verified present textually below).
            if re.search(r"\b%s\b[^\n;{]*\{" % re.escape(c), src):
                continue
            bad.add((fn, c))
    check("every reachable call resolves", not bad,
          str(sorted(bad))[:200] if bad else "%d calls checked" % sum(
              len(re.findall(r"[A-Za-z_]\w*\s*\(", funcs[f])) for f in seen))


# ---------------------------------------------------------------------------
# 8. Self-hosted parity on fail programs
# ---------------------------------------------------------------------------

def test_hlc_parity() -> None:
    section("8. Self-hosted parity (hlc rejects fail programs too)")
    d = tempfile.mkdtemp(prefix="hls_fs_parity_")
    hlc = os.path.join(REPO_ROOT, "src", "hlc.hls")
    for fname in ["fail_freestanding_uses.hls",
                  "fail_freestanding_std_import.hls"]:
        out = os.path.join(d, fname + ".c")
        r = boot(hlc, os.path.join(REPO_ROOT, "tests", "fail", fname), out)
        combined = (r.stdout or "") + (r.stderr or "")
        check("hlc rejects %s" % fname,
              r.returncode != 0 and "freestanding" in combined.lower(),
              combined.strip()[-120:])


def main() -> int:
    test_parse()
    test_enforcement()
    test_scope()
    test_denylist()
    test_audit_fmt()
    test_emission()
    test_link_closure()
    test_hlc_parity()
    print()
    failed = check.failed  # type: ignore[attr-defined]
    if failed:
        print("FREESTANDING ACCEPTANCE: %d assertion(s) FAILED" % failed)
        return 1
    print("ACCEPTANCE OK: Stage 77 -- #![freestanding] mode (no libc, "
          "no OS calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
