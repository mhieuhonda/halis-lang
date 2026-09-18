#!/usr/bin/env python3
"""Stage 78 nostd-acceptance test.

Verifies `#![no_std]` + the `core` module family end-to-end:

1. `core.` import resolution (Stage-0): all five modules resolve;
   traversal guards (`core.../x`, absolute) hold.
2. Core modules parse standalone (no `main` needed for parsing).
3. Stage-0 enforcement: the demo + ok-test check; std import, `uses`,
   and `extern` are rejected in `#![no_std]` mode.
4. Core API behavior on the interpreter (ok-test exits 0; targeted
   probes: parse_int edges, order-insensitive map equality,
   iterator exhaustion, clone independence).
5. Freestanding+core bridge: `#![freestanding]` importing
   `core.option` checks; `std`+`core` together conflict.
6. Self-hosted emission (hlc.hls through the interpreter): the
   no_std demo compiles to hosted C (`main`, stdio, core fns, no
   freestanding markers); parity rejections on fail programs.
7. hlfmt stability over every new Stage 77/78 file.
8. no_std is NOT freestanding (mode flags distinguish them).

Run::

    python3 tests/nostd_acceptance.py

(Called from ``make nostd-acceptance`` in mk/95-osdev.mk.)
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
DEMO = os.path.join(REPO_ROOT, "examples", "nostd_demo.hls")
OKTEST = os.path.join(REPO_ROOT, "tests", "ok", "feat_nostd_core.hls")
CORE = ["option", "result", "iter", "clone", "eq"]


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
# 1. core. resolution
# ---------------------------------------------------------------------------

def test_resolution() -> None:
    section("1. core. import resolution (+ traversal guards)")
    from boot.boot import _resolve_import
    entry = os.path.join(tempfile.mkdtemp(prefix="hls_ns_"), "e.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("fn main() -> int { return 0 }\n")
    for mod in CORE:
        p = _resolve_import("core." + mod, entry)
        check("core.%s resolves" % mod,
              p is not None and p.endswith(os.path.join("core", mod + ".hls")),
              p or "None")
    s = _resolve_import("std.str", entry)
    check("std.str still resolves",
          s is not None and s.endswith(os.path.join("std", "str.hls")))
    for evil in ["core.../secret", "core./x", "core"]:
        try:
            out = _resolve_import(evil, entry)
            check("guard %r" % evil, out is None, str(out)[:60])
        except SystemExit as ex:
            check("guard %r" % evil, True, str(ex)[:70])


# ---------------------------------------------------------------------------
# 2. Core modules parse standalone
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("2. Core modules parse standalone")
    from boot.lexer import tokenize
    from boot.parser import Parser
    for mod in CORE:
        path = os.path.join(REPO_ROOT, "core", mod + ".hls")
        with open(path, "rb") as f:
            prog = Parser(tokenize(f.read())).parse_program()
        kinds = (len(prog["structs"]), len(prog["enums"]), len(prog["fns"]))
        check("core/%s.hls parses (s=%d,e=%d,f=%d)" % ((mod,) + kinds),
              kinds != (0, 0, 0))
    check("core.option has no imports",
          Parser(tokenize(open(os.path.join(REPO_ROOT, "core", "option.hls"),
                                         "rb").read())).parse_program()["imports"] == [])
    it = Parser(tokenize(open(os.path.join(REPO_ROOT, "core", "iter.hls"),
                                        "rb").read())).parse_program()
    check("core.iter imports only core.option",
          [i["path"] for i in it["imports"]] == ["core.option"])


# ---------------------------------------------------------------------------
# 3. Stage-0 enforcement
# ---------------------------------------------------------------------------

def test_enforcement() -> None:
    section("3. Stage-0 enforcement (demo OK, violations rejected)")
    r = boot("--check", DEMO)
    check("nostd demo checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot("--check", OKTEST)
    check("nostd ok-test checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(DEMO)
    check("nostd demo runs (exit 0)", r.returncode == 0, "rc=%d" % r.returncode)
    r = boot(OKTEST)
    check("nostd ok-test runs (exit 0)", r.returncode == 0, "rc=%d" % r.returncode)
    for fname, needle in [
        ("fail_nostd_std_import.hls", "std module"),
        ("fail_nostd_uses.hls", "capabilities are unavailable"),
    ]:
        r = boot("--check", os.path.join(REPO_ROOT, "tests", "fail", fname))
        out = (r.stdout or "") + (r.stderr or "")
        check("%s rejected" % fname,
              r.returncode == 1 and needle in out, out.strip()[:90])
    p = write_tmp(".hls", "#![no_std]\nextern \"C\" {\n"
                           "    fn puts(s: str) -> int uses IO\n}\n"
                           "fn main() -> int { return 0 }\n")
    r = boot("--check", p)
    out = (r.stdout or "") + (r.stderr or "")
    check("no_std extern rejected",
          r.returncode == 1 and "not available in #![no_std]" in out,
          out.strip()[:90])
    r = boot("--audit", DEMO)
    check("--audit shows no_std",
          r.returncode == 0 and "#![no_std]" in (r.stdout or ""))


# ---------------------------------------------------------------------------
# 4. Core API behavior
# ---------------------------------------------------------------------------

def test_behavior() -> None:
    section("4. Core API behavior (targeted probes)")
    tmpl = ("#![no_std]\nimport \"core.%s\"\nfn main() -> int {\n%s}\n")
    probes = [
        ("parse_int edges", "result",
         'if result_unwrap(parse_int("-9223372036854775808")) != -9223372036854775808 { return 1 }\n'
         '    if !result_is_err(parse_int("12a")) { return 2 }\n'
         '    if !result_is_err(parse_int("--1")) { return 3 }\n'
         '    if result_unwrap_or(parse_int("7"), 0) != 7 { return 4 }\n'
         '    return 0'),
        ("iter exhaustion", "iter",
         'let xs: list[int] = [9]\n'
         '    let mut it: ListIter[int] = list_iter(xs)\n'
         '    if option_unwrap_or(iter_next(it), 0) != 9 { return 1 }\n'
         '    if iter_has_next(it) { return 2 }\n'
         '    if option_is_some(iter_next(it)) { return 3 }\n'
         '    if iter_remaining(it) != 0 { return 4 }\n'
         '    return 0'),
        ("clone independence", "clone",
         'import "core.option"\nfn main() -> int {\n'
         '    let a: list[int] = [1]\n'
         '    let b: list[int] = clone_list_of(a)\n'
         '    b.push(2)\n'
         '    if a.len() != 1 { return 1 }\n'
         '    return 0\n}\n'),
        ("map order-insensitive", "eq",
         'let m1: map[str, int] = map_new()\n'
         '    m1.set("a", 1)\n'
         '    let m2: map[str, int] = map_new()\n'
         '    m2.set("a", 1)\n'
         '    if !eq_map_str_int(m1, m2) { return 1 }\n'
         '    m2.set("b", 2)\n'
         '    if eq_map_str_int(m1, m2) { return 2 }\n'
         '    return 0'),
    ]
    for label, mod, body in probes:
        if label == "clone independence":
            src = body  # already a full program (needs two imports)
            src = src.replace('import "core.option"',
                              'import "core.option"\nimport "core.clone"')
            src = "#![no_std]\n" + src
        else:
            src = tmpl % (mod, body)
        p = write_tmp(".hls", src)
        r = boot(p)
        check(label, r.returncode == 0,
              "rc=%d %s" % (r.returncode,
                            ((r.stdout or "") + (r.stderr or "")).strip()[:80]))


# ---------------------------------------------------------------------------
# 5. Freestanding+core bridge + std/core duality
# ---------------------------------------------------------------------------

def test_bridge() -> None:
    section("5. Freestanding+core bridge; std/core duality")
    p = write_tmp(".hls", "#![freestanding]\nimport \"core.option\"\n"
                           "fn main() -> int {\n"
                           "    let o: Option[int] = Option.Some(3)\n"
                           "    return option_unwrap_or(o, 0)\n}\n")
    r = boot("--check", p)
    check("freestanding + core.option checks", r.returncode == 0)
    r = boot(p)
    check("freestanding + core.option runs (exit 3)", r.returncode == 3,
          "rc=%d" % r.returncode)
    p = write_tmp(".hls", "import \"std.option\"\nimport \"core.option\"\n"
                           "fn main() -> int { return 0 }\n")
    r = boot("--check", p)
    out = (r.stdout or "") + (r.stderr or "")
    check("std+core duality conflict",
          r.returncode == 1 and "duplicate type name" in out,
          out.strip()[:90])


# ---------------------------------------------------------------------------
# 6. Self-hosted emission + parity
# ---------------------------------------------------------------------------

def test_emission() -> None:
    section("6. Self-hosted emission (hosted no_std TU) + parity")
    d = tempfile.mkdtemp(prefix="hls_ns_emit_")
    out = os.path.join(d, "nostd.c")
    hlc = os.path.join(REPO_ROOT, "src", "hlc.hls")
    r = boot(hlc, DEMO, out)
    check("hlc emits no_std C", r.returncode == 0 and os.path.isfile(out),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    with open(out, encoding="utf-8", errors="replace") as f:
        src = f.read()
    check("hosted main kept", re.search(r"int main\s*\(", src) is not None)
    check("no _start entry", "void _start" not in src)
    check("stdio kept", "#include <stdio.h>" in src)
    check("no freestanding markers",
          "HL_FREESTANDING" not in src and "__builtin_trap" not in src)
    check("core fns compiled",
          "usf_option_unwrap" in src and "usf_iter_sum_int" in src
          and "usf_eq_map_str_int" in src and "usf_parse_int" in src)
    out2 = os.path.join(d, "f.c")
    r = boot(hlc, os.path.join(REPO_ROOT, "tests", "fail",
                               "fail_nostd_std_import.hls"), out2)
    combined = (r.stdout or "") + (r.stderr or "")
    check("hlc rejects no_std std-import",
          r.returncode != 0 and "no_std" in combined.lower(),
          combined.strip()[-120:])


# ---------------------------------------------------------------------------
# 7. hlfmt stability + mode distinction
# ---------------------------------------------------------------------------

def test_fmt_modes() -> None:
    section("7. hlfmt stability; no_std is not freestanding")
    import subprocess as sp
    for f in [DEMO, OKTEST,
              os.path.join(REPO_ROOT, "core", "option.hls"),
              os.path.join(REPO_ROOT, "core", "result.hls"),
              os.path.join(REPO_ROOT, "core", "iter.hls"),
              os.path.join(REPO_ROOT, "core", "clone.hls"),
              os.path.join(REPO_ROOT, "core", "eq.hls")]:
        r = sp.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                    "-c", f], capture_output=True, text=True,
                   cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean: %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""))
    from boot.boot import load_program as _load_program
    from boot.checker import check as checkmod
    d = tempfile.mkdtemp(prefix="hls_ns_modes_")
    p = os.path.join(d, "m.hls")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("#![no_std]\nfn main() -> int { return 0 }\n")
    c = checkmod(_load_program(p))
    check("no_std flags exact",
          c.crate_mode_flags == {"freestanding": False, "no_std": True},
          str(c.crate_mode_flags))


def main() -> int:
    test_resolution()
    test_parse()
    test_enforcement()
    test_behavior()
    test_bridge()
    test_emission()
    test_fmt_modes()
    print()
    failed = check.failed  # type: ignore[attr-defined]
    if failed:
        print("NOSTD ACCEPTANCE: %d assertion(s) FAILED" % failed)
        return 1
    print("ACCEPTANCE OK: Stage 78 -- #![no_std] + core modules "
          "(Option/Result/Iterator/Clone/Eq)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
