#!/usr/bin/env python3
"""Stage 80 mem-acceptance test.

Verifies `core.mem` — the physical-page allocator + page tables —
end-to-end:

1. `core.mem` resolves through `core.` prefix (Stage-0); traversal
   guards continue to hold for the new module.
2. The module parses standalone (structs, enums, fns, imports).
3. The module imports only `core.option` + `core.result` (the same
   freestanding-safe dependency closure as the rest of `core.*`).
4. Stage-0 enforcement: the demo + ok-test check and run on the
   interpreter (exit 0) in both `#![no_std]` and `#![freestanding]`
   modes; `extern` + `uses` are still rejected.
5. Targeted behavior probes (interpreter): page geometry and
   canonical-address math, FrameAlloc first-fit/reuse/OOM, run +
   huge-frame alignment, PTE flag/NO_EXECUTE/address-field round trip,
   space_map with on-demand tables billed to the allocator, atomic
   out-of-frames failure, translate/unmap/protect, huge map + unmap.
6. Self-hosted emission (hlc.hls through the interpreter): the
   no_std demo compiles to hosted C (main + libc kept, core.mem
   fns present, no freestanding markers); parity on the freestanding
   ok-test (native matches interpreter exit code).
7. hlfmt stability over every new Stage 80 file.
8. `--audit` reports the crate mode and shows every mem function
   pure (no `uses`).

Run::

    python3 tests/mem_acceptance.py

(Called from ``make mem-acceptance`` in mk/95-osdev.mk.)
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
DEMO = os.path.join(REPO_ROOT, "examples", "mem_demo.hls")
OKTEST = os.path.join(REPO_ROOT, "tests", "ok", "feat_stage80_mem.hls")
MEM = os.path.join(REPO_ROOT, "core", "mem.hls")
CORE = ["option", "result", "iter", "clone", "eq", "alloc", "mem"]


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
# 1. core.mem resolution + traversal guards
# ---------------------------------------------------------------------------

def test_resolution() -> None:
    section("1. core.mem import resolution (+ traversal guards)")
    from boot.boot import _resolve_import
    entry = os.path.join(tempfile.mkdtemp(prefix="hls_mem_"), "e.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("fn main() -> int { return 0 }\n")
    p = _resolve_import("core.mem", entry)
    check("core.mem resolves",
          p is not None and p.endswith(os.path.join("core", "mem.hls")),
          p or "None")
    # The existing core.* modules still resolve (regression check)
    for mod in CORE:
        p = _resolve_import("core." + mod, entry)
        check("core.%s still resolves" % mod,
              p is not None and p.endswith(os.path.join("core", mod + ".hls")))
    # std.str (hosted) still resolves alongside core.mem
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
# 2. core.mem parses standalone + imports are core-only
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("2. core.mem parses standalone + core-only imports")
    from boot.lexer import tokenize
    from boot.parser import Parser
    with open(MEM, "rb") as f:
        prog = Parser(tokenize(f.read())).parse_program()
    check("core/mem.hls parses",
          len(prog["structs"]) > 0 and len(prog["enums"]) > 0
          and len(prog["fns"]) > 0,
          "structs=%d, enums=%d, fns=%d"
          % (len(prog["structs"]), len(prog["fns"]), len(prog["enums"])))
    # Must declare the memory model's three public structs
    expected_structs = {"FrameAlloc", "PageTable", "AddressSpace"}
    check("all three structs present",
          expected_structs.issubset(set(prog["structs"].keys())),
          "missing: %s" % sorted(expected_structs - set(prog["structs"].keys())))
    check("MemError enum present",
          "MemError" in prog["enums"])
    # Imports: must be exactly `core.option` + `core.result` (the
    # freestanding-safe dependency closure; no `std.*`, no relative).
    imps = [i["path"] for i in prog["imports"]]
    check("imports are core-only",
          set(imps) == {"core.option", "core.result"},
          "got: %s" % imps)
    # Count key reference functions
    fns = set(prog["fns"].keys())
    must_have = ["mem_error_msg", "mem_page_size", "mem_page_align_up",
                 "mem_page_count", "vaddr_pml4_index", "vaddr_pt_index",
                 "vaddr_is_canonical", "pte_new", "pte_address",
                 "pte_is_present", "pte_is_no_execute",
                 "frame_new", "frame_alloc_one", "frame_alloc_run",
                 "frame_alloc_huge", "frame_free", "frame_reserve",
                 "table_new", "table_set_entry",
                 "space_new", "space_map", "space_map_huge",
                 "space_translate", "space_unmap", "space_unmap_huge",
                 "space_protect", "space_leaf_entry",
                 "mem_identity_map", "mem_map_bytes"]
    missing = [f for f in must_have if f not in fns]
    check("all reference functions present",
          not missing, "missing: %s" % missing)
    check("function count sane (>= 70)", len(fns) >= 70,
          "got %d" % len(fns))


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
    p = write_tmp(".hls", "#![no_std]\nimport \"core.mem\"\n"
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
        # Page geometry + canonical form (incl. the canonical hole)
        ("page geometry + canonical math", """
import "core.mem"
import "core.result"
fn main() -> int {
    if mem_page_size() != 4096 { return 1 }
    if mem_huge_page_size() != 2097152 { return 2 }
    if result_unwrap(mem_page_count(4097)) != 2 { return 3 }
    if !vaddr_is_canonical(-2147483648) { return 4 }
    if vaddr_is_canonical(140737488355328) { return 5 }
    if vaddr_pml4_index(-2147483648) != 511 { return 6 }
    if vaddr_pdpt_index(-2147483648) != 510 { return 7 }
    if vaddr_pd_index(2097152) != 1 { return 8 }
    if vaddr_page_offset(4096 + 291) != 291 { return 9 }
    if vaddr_huge_offset(2097152 + 12345) != 12345 { return 10 }
    return 0
}
"""),
        # FrameAlloc: first-fit, reuse, double-free, OOM
        ("frame alloc/free/reuse/OOM", """
import "core.mem"
import "core.result"
fn main() -> int {
    let fa: FrameAlloc = result_unwrap(frame_new(1048576, 4))
    if result_unwrap(frame_alloc_one(fa)) != 1048576 { return 1 }
    if result_unwrap(frame_alloc_one(fa)) != 1052672 { return 2 }
    if frame_used_count(fa) != 2 { return 3 }
    if result_is_err(frame_free(fa, 1048576, 1)) { return 4 }
    # first-fit hands the freed frame out again
    if result_unwrap(frame_alloc_one(fa)) != 1048576 { return 5 }
    # freeing it once more is legal; a THIRD free is a double free
    if result_is_err(frame_free(fa, 1048576, 1)) { return 6 }
    if !result_is_err(frame_free(fa, 1048576, 1)) { return 7 }
    if mem_error_msg(result_unwrap_err(frame_free(fa, 1048576, 1))) != "double free" { return 8 }
    if !result_is_err(frame_free(fa, 1048577, 1)) { return 9 }
    if !result_is_err(frame_free(fa, 1114112, 1)) { return 10 }
    let mut guard: int = 0
    while !frame_is_full(fa) && guard < 10 {
        let _x: Result[int, MemError] = frame_alloc_one(fa)
        guard = guard + 1
    }
    if !frame_is_full(fa) || !result_is_err(frame_alloc_one(fa)) { return 11 }
    return 0
}
"""),
        # Contiguous runs + 2-MiB-aligned huge frames
        ("frame runs + huge alignment", """
import "core.mem"
import "core.result"
fn main() -> int {
    let fa: FrameAlloc = result_unwrap(frame_new(1048576, 2048))
    let _r: Result[int, MemError] = frame_alloc_one(fa)
    if result_unwrap(frame_alloc_run(fa, 3)) != 1048576 + 4096 { return 1 }
    if !result_is_err(frame_alloc_run(fa, 0)) { return 2 }
    if result_unwrap(frame_alloc_huge(fa)) != 2097152 { return 3 }
    if result_unwrap(frame_alloc_huge(fa)) != 4194304 { return 4 }
    # 1 root alloc + 3 run frames + two huge runs of 512
    if frame_used_count(fa) != 1028 { return 5 }
    if result_is_err(frame_free(fa, 1048576, 1)) { return 6 }
    if result_is_err(frame_free(fa, 2097152, 512)) { return 7 }
    if result_unwrap(frame_alloc_huge(fa)) != 2097152 { return 8 }
    return 0
}
"""),
        # PTE round trip incl. the NX bit making entries negative
        ("PTE format + NX round trip", """
import "core.mem"
import "core.result"
fn main() -> int {
    let f: int = int_or(pte_flag_present(), int_or(pte_flag_writable(), pte_flag_no_execute()))
    let e: int = result_unwrap(pte_new(32768, f))
    if e >= 0 { return 1 }
    if pte_address(e) != 32768 { return 2 }
    if !pte_is_no_execute(e) || !pte_is_writable(e) || !pte_is_present(e) { return 3 }
    let e2: int = pte_clear_flags(e, pte_flag_no_execute())
    if pte_is_no_execute(e2) || pte_address(e2) != 32768 { return 4 }
    if !result_is_err(pte_new(4095, 0)) { return 5 }
    if !result_is_err(pte_new(4503599627370496, 0)) { return 6 }
    if !result_is_err(pte_new(0, 4096)) { return 7 }
    return 0
}
"""),
        # space_map bills table frames to the allocator; translate
        # follows the full 4-level walk
        ("space map + on-demand tables + translate", """
import "core.mem"
import "core.result"
fn main() -> int {
    let fa: FrameAlloc = result_unwrap(frame_new(1048576, 64))
    let root: int = result_unwrap(frame_alloc_one(fa))
    let sp: AddressSpace = result_unwrap(space_new(root))
    let mr: Result[int, MemError] = space_map(fa, sp, 2097152, 3145728, 2, pte_flag_writable())
    if result_is_err(mr) { return 1 }
    if space_table_count(sp) != 4 { return 2 }
    if frame_used_count(fa) != 4 { return 3 }
    if option_unwrap(space_translate(sp, 2097152 + 100)) != 3145828 { return 4 }
    if option_is_some(space_translate(sp, 2097152 + 8192)) { return 5 }
    if !result_is_err(space_map(fa, sp, 2097152, 3145728, 1, 0)) { return 6 }
    return 0
}
"""),
        # Atomic failure: OOM leaves space + allocator untouched
        ("map atomicity under frame exhaustion", """
import "core.mem"
import "core.result"
fn main() -> int {
    let fa: FrameAlloc = result_unwrap(frame_new(9437184, 2))
    let root: int = result_unwrap(frame_alloc_one(fa))
    let sp: AddressSpace = result_unwrap(space_new(root))
    let oom: Result[int, MemError] = space_map(fa, sp, 2097152, 3145728, 1, 0)
    if !result_is_err(oom) { return 1 }
    if mem_error_msg(result_unwrap_err(oom)) != "out of frames" { return 2 }
    if space_table_count(sp) != 1 { return 3 }
    if frame_used_count(fa) != 1 { return 4 }
    if space_maps(sp) != 0 { return 5 }
    return 0
}
"""),
        # unmap + protect (W^X re-flag) with address preservation
        ("unmap + protect re-flag", """
import "core.mem"
import "core.result"
fn main() -> int {
    let fa: FrameAlloc = result_unwrap(frame_new(1048576, 64))
    let root: int = result_unwrap(frame_alloc_one(fa))
    let sp: AddressSpace = result_unwrap(space_new(root))
    let _m: Result[int, MemError] = space_map(fa, sp, 2097152, 3145728, 1, pte_flag_writable())
    if result_is_err(space_protect(sp, 2097152, 1, pte_flag_present())) { return 1 }
    let l: int = result_unwrap(space_leaf_entry(sp, 2097152))
    if pte_is_writable(l) || pte_address(l) != 3145728 { return 2 }
    if result_is_err(space_unmap(sp, 2097152, 1)) { return 3 }
    if option_is_some(space_translate(sp, 2097152)) { return 4 }
    if !result_is_err(space_unmap(sp, 2097152, 1)) { return 5 }
    if !result_is_err(space_protect(sp, 2097152, 1, pte_flag_present())) { return 6 }
    return 0
}
"""),
        # Huge map at the PD level + paired huge unmap
        ("huge map + huge unmap", """
import "core.mem"
import "core.result"
fn main() -> int {
    let fa: FrameAlloc = result_unwrap(frame_new(1048576, 2048))
    let root: int = result_unwrap(frame_alloc_one(fa))
    let sp: AddressSpace = result_unwrap(space_new(root))
    let hp: int = result_unwrap(frame_alloc_huge(fa))
    if hp != 2097152 { return 1 }
    let hv: int = -140737488355328
    let f: int = int_or(pte_flag_writable(), pte_flag_no_execute())
    if result_is_err(space_map_huge(fa, sp, hv, hp, 1, f)) { return 2 }
    if space_table_count(sp) != 3 { return 3 }
    if option_unwrap(space_translate(sp, hv + 12345)) != hp + 12345 { return 4 }
    if !pte_is_huge(result_unwrap(space_leaf_entry(sp, hv))) { return 5 }
    if !result_is_err(space_map(fa, sp, hv, 4096, 1, 0)) { return 6 }
    if !result_is_err(space_unmap(sp, hv, 1)) { return 7 }
    if result_is_err(space_unmap_huge(sp, hv, 1)) { return 8 }
    if option_is_some(space_translate(sp, hv)) { return 9 }
    if !result_is_err(space_unmap_huge(sp, hv, 1)) { return 10 }
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
    d = tempfile.mkdtemp(prefix="hls_mem_emit_")
    out = os.path.join(d, "mem.c")
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
    check("core.mem fns compiled",
          all(s in src for s in (
              "usf_frame_new", "usf_frame_alloc_one", "usf_frame_alloc_huge",
              "usf_frame_free", "usf_space_new", "usf_space_map",
              "usf_space_map_huge", "usf_space_translate",
              "usf_space_unmap", "usf_space_protect",
              "usf_mem_identity_map", "usf_mem_map_bytes")))
    # Compile the emitted C and run it; the exit code must match
    # the interpreter's (0).
    if subprocess.run(["gcc", "-O2", "-o", os.path.join(d, "mem.bin"),
                        out, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "mem.bin")],
                             capture_output=True, timeout=30)
        check("native mem_demo exit 0",
              nat.returncode == 0, "rc=%d" % nat.returncode)
    else:
        check("native mem_demo links", False, "gcc failed")
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
    section("6. hlfmt stability over every Stage 80 file")
    import subprocess as sp
    files = [MEM, DEMO, OKTEST]
    for f in files:
        r = sp.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                    "-c", f], capture_output=True, text=True,
                   cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean: %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""),
              (r.stdout or "").strip()[:80])


# ---------------------------------------------------------------------------
# 7. --audit shows every mem function pure
# ---------------------------------------------------------------------------

def test_audit() -> None:
    section("7. --audit: every mem function pure (no uses)")
    r = boot("--audit", DEMO)
    out = r.stdout or ""
    check("audit ran", r.returncode == 0)
    # Every function from core.mem must appear as "(none - pure)"
    # (no `uses` clause). Pull the function-name column and confirm
    # the mem fns are listed with the pure marker.
    must_be_pure = [
        "mem_error_msg", "mem_page_size", "mem_huge_page_size",
        "mem_pages_per_huge", "mem_is_page_aligned", "mem_is_huge_aligned",
        "mem_page_align_up", "mem_page_count", "mem_pages_to_bytes",
        "vaddr_pml4_index", "vaddr_pdpt_index", "vaddr_pd_index",
        "vaddr_pt_index", "vaddr_page_offset", "vaddr_huge_offset",
        "vaddr_is_canonical", "vaddr_is_huge_aligned",
        "pte_flag_present", "pte_flag_writable", "pte_flag_user",
        "pte_flag_no_execute", "pte_flags_known_mask", "pte_addr_limit",
        "pte_new", "pte_address", "pte_flags", "pte_is_present",
        "pte_is_writable", "pte_is_huge", "pte_is_no_execute",
        "pte_set_flags", "pte_clear_flags",
        "frame_new", "frame_alloc_one", "frame_alloc_run",
        "frame_alloc_huge", "frame_free", "frame_reserve",
        "frame_index_of", "frame_in_range", "frame_is_full",
        "frame_used_count", "frame_free_count", "frame_peak",
        "table_new", "table_entry", "table_set_entry",
        "table_present_count", "space_new", "space_table_count",
        "space_get_entry", "space_set_entry", "space_leaf_entry",
        "space_translate", "space_is_mapped", "space_map",
        "space_map_huge", "space_unmap", "space_unmap_huge",
        "space_protect", "mem_identity_map", "mem_map_bytes",
    ]
    missing = [f for f in must_be_pure if f not in out]
    check("all mem fns in audit output", not missing,
          "missing: %s" % missing[:5])
    bad = []
    for line in out.splitlines():
        s = line.strip()
        for f in must_be_pure:
            if s.startswith(f + " "):
                if "(none - pure)" not in s and "(none)" not in s:
                    bad.append(s)
                break
    check("all mem fns declared pure", not bad,
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
        print("MEM ACCEPTANCE: %d assertion(s) FAILED" % failed)
        return 1
    print("ACCEPTANCE OK: Stage 80 -- core.mem "
          "(frame allocator + page tables)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
