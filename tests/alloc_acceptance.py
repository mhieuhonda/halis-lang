#!/usr/bin/env python3
"""Stage 79 alloc-acceptance test.

Verifies `core.alloc` — the pluggable allocator protocol — end-to-end:

1. `core.alloc` resolves through `core.` prefix (Stage-0); traversal
   guards continue to hold for the new module.
2. The module parses standalone (structs, enums, fns, imports).
3. The module imports only `core.option` + `core.result` (the same
   freestanding-safe dependency closure as the rest of `core.*`).
4. Stage-0 enforcement: the demo + ok-test check and run on the
   interpreter (exit 0) in both `#![no_std]` and `#![freestanding]`
   modes; `extern` + `uses` are still rejected.
5. Targeted behavior probes (interpreter): layout validation,
   BumpAlloc sequential offsets + peak + reset, PoolAlloc LIFO
   recycle + double-free detection, NullAlloc sentinel, AllocStats
   accounting + peak watermark.
6. Self-hosted emission (hlc.hls through the interpreter): the
   no_std demo compiles to hosted C (main + libc kept, core.alloc
   fns present, no freestanding markers); parity on the freestanding
   ok-test (native matches interpreter exit code).
7. hlfmt stability over every new Stage 79 file.
8. `--audit` reports the crate mode and shows every alloc function
   pure (no `uses`).

Run::

    python3 tests/alloc_acceptance.py

(Called from ``make alloc-acceptance`` in mk/95-osdev.mk.)
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
DEMO = os.path.join(REPO_ROOT, "examples", "alloc_demo.hls")
OKTEST = os.path.join(REPO_ROOT, "tests", "ok", "feat_stage79_alloc.hls")
ALLOC = os.path.join(REPO_ROOT, "core", "alloc.hls")
CORE = ["option", "result", "iter", "clone", "eq", "alloc"]


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
# 1. core.alloc resolution + traversal guards
# ---------------------------------------------------------------------------

def test_resolution() -> None:
    section("1. core.alloc import resolution (+ traversal guards)")
    from boot.boot import _resolve_import
    entry = os.path.join(tempfile.mkdtemp(prefix="hls_alloc_"), "e.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("fn main() -> int { return 0 }\n")
    p = _resolve_import("core.alloc", entry)
    check("core.alloc resolves",
          p is not None and p.endswith(os.path.join("core", "alloc.hls")),
          p or "None")
    # The existing core.* modules still resolve (regression check)
    for mod in CORE:
        p = _resolve_import("core." + mod, entry)
        check("core.%s still resolves" % mod,
              p is not None and p.endswith(os.path.join("core", mod + ".hls")))
    # std.str (hosted) still resolves alongside core.alloc
    s = _resolve_import("std.str", entry)
    check("std.str still resolves",
          s is not None and s.endswith(os.path.join("std", "str.hls")))
    # Path traversal guards: the new module doesn't open any new
    # vector. The same three evil paths from the Stage 78 suite
    # continue to be rejected.
    for evil in ["core.../secret", "core./x", "core"]:
        try:
            out = _resolve_import(evil, entry)
            check("guard %r" % evil, out is None, str(out)[:60])
        except SystemExit as ex:
            check("guard %r" % evil, True, str(ex)[:70])


# ---------------------------------------------------------------------------
# 2. core.alloc parses standalone + imports are core-only
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("2. core.alloc parses standalone + core-only imports")
    from boot.lexer import tokenize
    from boot.parser import Parser
    with open(ALLOC, "rb") as f:
        prog = Parser(tokenize(f.read())).parse_program()
    check("core/alloc.hls parses",
          len(prog["structs"]) > 0 and len(prog["enums"]) > 0
          and len(prog["fns"]) > 0,
          "structs=%d, enums=%d, fns=%d"
          % (len(prog["structs"]), len(prog["enums"]), len(prog["fns"])))
    # Must declare the Alloc protocol's five public types
    expected_structs = {"Layout", "BumpAlloc", "PoolAlloc", "NullAlloc",
                        "AllocStats"}
    check("all five structs present",
          expected_structs.issubset(set(prog["structs"].keys())),
          "missing: %s" % sorted(expected_structs - set(prog["structs"].keys())))
    check("AllocError enum present",
          "AllocError" in prog["enums"])
    # Imports: must be exactly `core.option` + `core.result` (the
    # freestanding-safe dependency closure; no `std.*`, no relative).
    imps = [i["path"] for i in prog["imports"]]
    check("imports are core-only",
          set(imps) == {"core.option", "core.result"},
          "got: %s" % imps)
    # Count key reference functions
    fns = set(prog["fns"].keys())
    must_have = ["layout_new", "layout_pad_to", "layout_align_up",
                 "bump_new", "bump_alloc", "bump_dealloc", "bump_reset",
                 "pool_new", "pool_alloc", "pool_dealloc",
                 "null_alloc_new", "null_alloc_alloc",
                 "stats_new", "stats_record_alloc", "stats_record_dealloc",
                 "alloc_error_msg"]
    missing = [f for f in must_have if f not in fns]
    check("all reference functions present",
          not missing, "missing: %s" % missing)


# ---------------------------------------------------------------------------
# 3. Stage-0 enforcement (no_std + freestanding)
# ---------------------------------------------------------------------------

def test_enforcement() -> None:
    section("3. Stage-0 enforcement (demo + ok-test pass in both modes)")
    r = boot("--check", DEMO)
    check("no_std demo checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot("--check", OKTEST)
    check("no_std ok-test checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(DEMO)
    check("no_std demo runs (exit 0)", r.returncode == 0,
          "rc=%d" % r.returncode)
    r = boot(OKTEST)
    check("no_std ok-test runs (exit 0)", r.returncode == 0,
          "rc=%d" % r.returncode)
    # Same ok-test with #![freestanding] instead of #![no_std] —
    # the C backend will emit `_start` and link with -nostdlib; the
    # exit code is the program's answer (the same 0). Replace ONLY
    # the standalone crate attribute (the comment headers also
    # contain the tokens, so a naive .replace of the FIRST match
    # would corrupt the comment instead of switching the crate).
    fs_src = open(OKTEST, encoding="utf-8").read().replace(
        "\n#![no_std]\n", "\n#![freestanding]\n", 1)
    p = write_tmp(".hls", fs_src)
    r = boot("--check", p)
    check("freestanding ok-test checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(p)
    check("freestanding ok-test runs (exit 0)",
          r.returncode == 0, "rc=%d" % r.returncode)
    # The same ok-test with NO crate attr — the std.* boundary
    # is closed but core.* is fine; pure language programs run on
    # the hosted toolchain too.
    bare_src = open(OKTEST, encoding="utf-8").read().replace(
        "#![no_std]\n", "", 1)
    p = write_tmp(".hls", bare_src)
    r = boot(p)
    check("hosted (no crate attr) ok-test runs (exit 0)",
          r.returncode == 0, "rc=%d" % r.returncode)
    # --audit reports the crate mode
    r = boot("--audit", DEMO)
    check("--audit shows no_std",
          r.returncode == 0 and "#![no_std]" in (r.stdout or ""))
    # extern + uses are still rejected in no_std mode
    p = write_tmp(".hls", "#![no_std]\nimport \"core.alloc\"\n"
                   "extern \"C\" {\n    fn malloc(n: int) -> int uses IO\n}\n"
                   "fn main() -> int { return 0 }\n")
    r = boot("--check", p)
    out = (r.stdout or "") + (r.stderr or "")
    check("no_std extern rejected",
          r.returncode == 1 and "not available in #![no_std]" in out,
          out.strip()[:90])


# ---------------------------------------------------------------------------
# 4. Targeted behavior probes (interpreter, no_std mode)
# ---------------------------------------------------------------------------

def test_behavior() -> None:
    section("4. Behavior probes (interpreter, no_std mode)")
    # A series of single-feature probes; each exits 0 on pass.
    probes = [
        # Layout validation: zero size, non-pow2 align, oversized align
        # are all rejected; valid layouts round-trip through accessors.
        ("layout validation", """
import "core.alloc"
import "core.result"
fn main() -> int {
    if !result_is_err(layout_new(0, 8)) { return 1 }
    if !result_is_err(layout_new(16, 7)) { return 2 }
    if !result_is_err(layout_new(16, 8192)) { return 3 }
    let l: Layout = result_unwrap(layout_new(24, 8))
    if layout_size(l) != 24 { return 4 }
    if layout_align(l) != 8 { return 5 }
    if layout_pad_to(l) != 24 { return 6 }
    let l4: Layout = result_unwrap(layout_new(4, 4))
    if layout_pad_to(l4) != 4 { return 7 }
    let l5: Layout = result_unwrap(layout_new(5, 4))
    if layout_pad_to(l5) != 8 { return 8 }
    return 0
}
"""),
        # BumpAlloc sequential offsets, peak tracking, reset, OOM
        ("bump alloc + reset + OOM", """
import "core.alloc"
import "core.result"
fn main() -> int {
    let a: BumpAlloc = bump_new(32)
    let l8: Layout = result_unwrap(layout_new(8, 8))
    let p1: int = result_unwrap(bump_alloc(a, l8))
    if p1 != 0 { return 1 }
    let p2: int = result_unwrap(bump_alloc(a, l8))
    if p2 != 8 { return 2 }
    let p3: int = result_unwrap(bump_alloc(a, l8))
    if p3 != 16 { return 3 }
    if bump_used(a) != 24 { return 4 }
    if bump_peak(a) != 24 { return 5 }
    if bump_allocations(a) != 3 { return 6 }
    bump_reset(a)
    if bump_used(a) != 0 { return 7 }
    if bump_peak(a) != 24 { return 8 }
    if bump_allocations(a) != 3 { return 9 }
    let small: BumpAlloc = bump_new(8)
    let _p: int = result_unwrap(bump_alloc(small, l8))
    if !result_is_err(bump_alloc(small, l8)) { return 10 }
    if !bump_full(small) { return 11 }
    return 0
}
"""),
        # BumpAlloc alignment: 4-byte layout advances 4, then 8-byte
        # layout rounds the cursor UP to 8 before allocating. After
        # four allocations the used count is 32: 4 (l4) + 8 (l8) +
        # 4 (l4) + 8 (l8) + 4 padding (the second l8 round-up from
        # 20 to 24). The padding bytes are accounted in `bump_used`
        # because the cursor advances through them, even though no
        # allocation lives in them.
        ("bump alignment padding", """
import "core.alloc"
import "core.result"
fn main() -> int {
    let a: BumpAlloc = bump_new(64)
    let l4: Layout = result_unwrap(layout_new(4, 4))
    let l8: Layout = result_unwrap(layout_new(8, 8))
    let p1: int = result_unwrap(bump_alloc(a, l4))
    if p1 != 0 { return 1 }
    let p2: int = result_unwrap(bump_alloc(a, l8))
    if p2 != 8 { return 2 }
    let p3: int = result_unwrap(bump_alloc(a, l4))
    if p3 != 16 { return 3 }
    let p4: int = result_unwrap(bump_alloc(a, l8))
    if p4 != 24 { return 4 }
    if bump_used(a) != 32 { return 5 }
    return 0
}
"""),
        # PoolAlloc LIFO recycle
        ("pool LIFO recycle", """
import "core.alloc"
import "core.result"
fn main() -> int {
    let pool: PoolAlloc = pool_new(16, 3)
    let l: Layout = result_unwrap(layout_new(16, 16))
    let b1: int = result_unwrap(pool_alloc(pool, l))
    if b1 != 0 { return 1 }
    let b2: int = result_unwrap(pool_alloc(pool, l))
    if b2 != 16 { return 2 }
    let b3: int = result_unwrap(pool_alloc(pool, l))
    if b3 != 32 { return 3 }
    if !pool_full(pool) { return 4 }
    if result_is_err(pool_dealloc(pool, b2, l)) { return 5 }
    let b4: int = result_unwrap(pool_alloc(pool, l))
    if b4 != 16 { return 6 }
    if !result_is_err(pool_alloc(pool, l)) { return 7 }
    return 0
}
"""),
        # PoolAlloc double-free + out-of-range detection
        ("pool double-free + range", """
import "core.alloc"
import "core.result"
fn main() -> int {
    let pool: PoolAlloc = pool_new(16, 2)
    let l: Layout = result_unwrap(layout_new(16, 16))
    let b1: int = result_unwrap(pool_alloc(pool, l))
    if result_is_err(pool_dealloc(pool, b1, l)) { return 1 }
    let dbl: Result[int, AllocError] = pool_dealloc(pool, b1, l)
    if !result_is_err(dbl) { return 2 }
    if alloc_error_msg(result_unwrap_err(dbl)) != "double free" { return 3 }
    if !result_is_err(pool_dealloc(pool, 1000, l)) { return 4 }
    if !result_is_err(pool_dealloc(pool, -1, l)) { return 5 }
    if !result_is_err(pool_dealloc(pool, 7, l)) { return 6 }
    return 0
}
"""),
        # NullAlloc: always Err
        ("null alloc sentinel", """
import "core.alloc"
import "core.result"
fn main() -> int {
    let na: NullAlloc = null_alloc_new()
    let l: Layout = result_unwrap(layout_new(8, 8))
    if !result_is_err(null_alloc_alloc(na, l)) { return 1 }
    if !result_is_err(null_alloc_dealloc(na, 0, l)) { return 2 }
    return 0
}
"""),
        # AllocStats: cumulative + peak watermark
        ("stats accounting + peak", """
import "core.alloc"
import "core.result"
fn main() -> int {
    let s: AllocStats = stats_new()
    if stats_current(s) != 0 { return 1 }
    stats_record_alloc(s, 100)
    if stats_current(s) != 100 { return 2 }
    if stats_peak(s) != 100 { return 3 }
    stats_record_alloc(s, 50)
    if stats_current(s) != 150 { return 4 }
    if stats_peak(s) != 150 { return 5 }
    stats_record_dealloc(s, 100)
    if stats_current(s) != 50 { return 6 }
    if stats_peak(s) != 150 { return 7 }
    if stats_allocations(s) != 2 { return 8 }
    if stats_deallocations(s) != 1 { return 9 }
    return 0
}
"""),
    ]
    for label, body in probes:
        src = "#![no_std]\n" + body
        p = write_tmp(".hls", src)
        r = boot(p)
        check(label, r.returncode == 0,
              "rc=%d %s" % (r.returncode,
                            ((r.stdout or "") + (r.stderr or "")).strip()[:80]))


# ---------------------------------------------------------------------------
# 5. Self-hosted emission + parity
# ---------------------------------------------------------------------------

def test_emission() -> None:
    section("5. Self-hosted emission (hlc) + native parity")
    d = tempfile.mkdtemp(prefix="hls_alloc_emit_")
    out = os.path.join(d, "alloc.c")
    hlc = os.path.join(REPO_ROOT, "src", "hlc.hls")
    r = boot(hlc, DEMO, out)
    check("hlc emits no_std C",
          r.returncode == 0 and os.path.isfile(out),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    with open(out, encoding="utf-8", errors="replace") as f:
        src = f.read()
    check("hosted main kept", re.search(r"int main\s*\(", src) is not None)
    check("no _start entry", "void _start" not in src)
    check("stdio kept", "#include <stdio.h>" in src)
    check("no freestanding markers",
          "HL_FREESTANDING" not in src and "__builtin_trap" not in src)
    check("core.alloc fns compiled",
          all(s in src for s in (
              "usf_layout_new", "usf_bump_alloc", "usf_bump_reset",
              "usf_pool_alloc", "usf_pool_dealloc",
              "usf_null_alloc_alloc", "usf_stats_record_alloc",
              "usf_alloc_error_msg")))
    # Compile the emitted C and run it; the exit code must match
    # the interpreter's (0).
    if subprocess.run(["gcc", "-O2", "-o", os.path.join(d, "alloc.bin"),
                        out, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "alloc.bin")],
                             capture_output=True, timeout=30)
        check("native alloc_demo exit 0",
              nat.returncode == 0, "rc=%d" % nat.returncode)
    else:
        check("native alloc_demo links", False, "gcc failed")
    # Freestanding parity: the same ok-test with #![freestanding]
    # instead of #![no_std] must compile to a -nostdlib binary that
    # exits with the same code as the interpreter. Replace ONLY the
    # standalone crate attribute (see test_enforcement for the
    # comment-corruption caveat).
    fs_src = open(OKTEST, encoding="utf-8").read().replace(
        "\n#![no_std]\n", "\n#![freestanding]\n", 1)
    fs_p = write_tmp(".hls", fs_src)
    interp = boot(fs_p)
    out2 = os.path.join(d, "fs.c")
    r = boot(hlc, fs_p, out2)
    check("hlc emits freestanding C",
          r.returncode == 0 and os.path.isfile(out2),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    if subprocess.run(["gcc", "-O2", "-ffreestanding", "-nostdlib",
                        "-ffunction-sections", "-fno-stack-protector",
                        "-Wl,--gc-sections",
                        "-o", os.path.join(d, "fs.bin"), out2],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "fs.bin")],
                             capture_output=True, timeout=30)
        check("freestanding native == interpreter",
              nat.returncode == interp.returncode,
              "interp=%d nat=%d" % (interp.returncode, nat.returncode))
    else:
        check("freestanding binary links", False, "gcc failed")


# ---------------------------------------------------------------------------
# 6. hlfmt stability
# ---------------------------------------------------------------------------

def test_fmt() -> None:
    section("6. hlfmt stability over every Stage 79 file")
    import subprocess as sp
    files = [ALLOC, DEMO, OKTEST]
    for f in files:
        r = sp.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                    "-c", f], capture_output=True, text=True,
                   cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean: %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""),
              (r.stdout or "").strip()[:80])


# ---------------------------------------------------------------------------
# 7. --audit shows every alloc function pure
# ---------------------------------------------------------------------------

def test_audit() -> None:
    section("7. --audit: every alloc function pure (no uses)")
    r = boot("--audit", DEMO)
    out = r.stdout or ""
    check("audit ran", r.returncode == 0)
    # Every function from core.alloc must appear as "(none - pure)"
    # (no `uses` clause). Pull the function-name column and confirm
    # the alloc fns are listed with the pure marker.
    must_be_pure = [
        "alloc_error_msg", "layout_is_power_of_two", "layout_new",
        "layout_for_words", "layout_align_up", "layout_size", "layout_align",
        "layout_pad_to", "layout_resize",
        "bump_new", "bump_alloc", "bump_dealloc", "bump_reset", "bump_used",
        "bump_peak", "bump_allocations", "bump_capacity", "bump_full",
        "pool_new", "pool_alloc", "pool_dealloc", "pool_free_count",
        "pool_used_count", "pool_total", "pool_block_size", "pool_peak",
        "pool_allocations", "pool_full",
        "null_alloc_new", "null_alloc_alloc", "null_alloc_dealloc",
        "stats_new", "stats_record_alloc", "stats_record_dealloc",
        "stats_bytes_alloc", "stats_bytes_dealloc", "stats_current",
        "stats_peak", "stats_allocations", "stats_deallocations",
    ]
    missing = [f for f in must_be_pure if f not in out]
    check("all alloc fns in audit output", not missing,
          "missing: %s" % missing[:5])
    bad = []
    for line in out.splitlines():
        s = line.strip()
        for f in must_be_pure:
            if s.startswith(f + " "):
                if "(none - pure)" not in s and "(none)" not in s:
                    bad.append(s)
                break
    check("all alloc fns declared pure", not bad,
          "impure: %s" % bad[:3])
    # Crate mode reported
    check("crate mode reported",
          "Crate mode:" in out and "#![no_std]" in out)


def main() -> int:
    test_resolution()
    test_parse()
    test_enforcement()
    test_behavior()
    test_emission()
    test_fmt()
    test_audit()
    print()
    failed = check.failed  # type: ignore[attr-defined]
    if failed:
        print("ALLOC ACCEPTANCE: %d assertion(s) FAILED" % failed)
        return 1
    print("ACCEPTANCE OK: Stage 79 -- core.alloc "
          "(Layout + AllocError + BumpAlloc + PoolAlloc + NullAlloc + AllocStats)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
