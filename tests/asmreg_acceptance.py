#!/usr/bin/env python3
"""Stage 83 asmreg-acceptance test.

Verifies the inline-asm register constraints end to end:

1. `core.asm` resolves through `core.` prefix (Stage-0); traversal
   guards continue to hold for the new module.
2. The module parses standalone (enum, fns, zero imports — the
   freestanding-safe closure needs nothing else).
3. Stage-0 enforcement: the no_std ok-test checks and runs (exit 0);
   the demo is REJECTED by the interpreter (it executes asm!, which
   only native code can run) and the ten fail programs are rejected
   with the Stage 83 messages.
4. Targeted probes: a battery of valid register-constraint programs
   is accepted by boot; every diagnostic class (width, owner,
   overlap, duplicate, reserved, SSE float) fires with its exact
   message.
5. Self-hosted emission (hlc.hls through the interpreter): the demo
   compiles to C carrying local register variables and explicit
   clobbers, links -Werror and prints DEMO OK; the ok-test compiles
   hosted and freestanding (-nostdlib) with exit 0; boot/hlc parity
   on the fail programs.
6. hlfmt stability over every new Stage 83 file.
7. `--audit` shows every core.asm function pure (no `uses`).

Run::

    python3 tests/asmreg_acceptance.py

(Called from ``make asmreg-acceptance`` in mk/95-osdev.mk.)
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
DEMO = os.path.join(REPO_ROOT, "examples", "asmreg_demo.hls")
OKTEST = os.path.join(REPO_ROOT, "tests", "ok", "feat_stage83_asmreg.hls")
ASM = os.path.join(REPO_ROOT, "core", "asm.hls")
FAILS = sorted(
    os.path.join(REPO_ROOT, "tests", "fail", f)
    for f in os.listdir(os.path.join(REPO_ROOT, "tests", "fail"))
    if f.startswith("fail_asmreg_"))
CORE = ["option", "result", "iter", "clone", "eq", "alloc", "mem",
        "panic", "stack", "asm"]
GCC = os.environ.get("CC", "gcc")


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
                          cwd=cwd or REPO_ROOT, timeout=600)


def write_tmp(suffix: str, src: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return path


# ---------------------------------------------------------------------------
# 1. core.asm resolution + traversal guards
# ---------------------------------------------------------------------------

def test_resolution() -> None:
    section("1. core.asm import resolution (+ traversal guards)")
    from boot.boot import _resolve_import
    entry = os.path.join(tempfile.mkdtemp(prefix="hls_asmreg_"), "e.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("fn main() -> int { return 0 }\n")
    p = _resolve_import("core.asm", entry)
    check("core.asm resolves",
          p is not None and p.endswith(os.path.join("core", "asm.hls")),
          p or "None")
    for mod in CORE:
        p = _resolve_import("core." + mod, entry)
        check("core.%s still resolves" % mod,
              p is not None and p.endswith(os.path.join("core", mod + ".hls")))
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
# 2. core.asm parses standalone
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("2. core.asm parses standalone (enum + fns, zero imports)")
    from boot.lexer import tokenize
    from boot.parser import Parser
    with open(ASM, "rb") as f:
        prog = Parser(tokenize(f.read())).parse_program()
    check("core/asm.hls parses",
          len(prog["enums"]) > 0 and len(prog["fns"]) > 0,
          "enums=%d, fns=%d" % (len(prog["enums"]), len(prog["fns"])))
    check("AsmFault enum present", "AsmFault" in prog["enums"])
    imps = [i["path"] for i in prog["imports"]]
    check("imports are core-only",
          all(i.startswith("core.") for i in imps), "got: %s" % imps)
    fns = set(prog["fns"].keys())
    must_have = [
        "asm64_gp_regs", "asm64_sse_regs", "asm_reg_width",
        "asm_reg_is_sse", "asm_reg_base", "asm_regs_overlap",
        "asm_valid_clobber", "asm_clobber_fault", "asm_operand_conflict",
        "asm_fault_name", "asm_fault_code", "asm_sysv_arg_reg",
        "asm_syscall_arg_reg", "asm_syscall_clobbers", "asm_sysv_ret_reg",
        "asm_sysv_fp_ret_reg", "asm_caller_saved", "asm_callee_saved",
        "asm_regs_to_save", "asm_scratch_reg", "asm_check_invariants",
    ]
    missing = [f for f in must_have if f not in fns]
    check("all reference functions present",
          not missing, "missing: %s" % missing)
    check("function count sane (>= 21)", len(fns) >= 21,
          "got %d" % len(fns))


# ---------------------------------------------------------------------------
# 3. Stage-0 enforcement
# ---------------------------------------------------------------------------

def test_enforcement() -> None:
    section("3. Stage-0 enforcement (ok-test clean; demo native-only; fails rejected)")
    r = boot("--check", OKTEST)
    check("no_std ok-test checks", r.returncode == 0,
          (r.stdout or "").strip()[:80])
    r = boot(OKTEST)
    check("no_std ok-test runs (exit 0)", r.returncode == 0,
          "rc=%d" % r.returncode)
    # The demo EXECUTES asm! — the interpreter must refuse (that is the
    # documented Stage 27 behaviour; the demo runs natively, section 5).
    r = boot(DEMO)
    out = (r.stdout or "") + (r.stderr or "")
    check("demo refused by the interpreter (asm! executed)",
          r.returncode != 0
          and "asm! cannot be executed by the boot interpreter" in out,
          "rc=%d" % r.returncode)
    r = boot("--check", DEMO)
    check("demo checks (asm! is a declared-statement parse)",
          r.returncode == 0, (r.stdout or "").strip()[:80])
    # Every Stage 83 fail program is rejected with an asm! diagnostic.
    for path in FAILS:
        r = boot("--check", path)
        out = (r.stdout or "") + (r.stderr or "")
        check("rejected: %s" % os.path.basename(path),
              r.returncode == 1 and "asm!" in out,
              out.strip().splitlines()[0][:100] if out.strip() else "no output")


# ---------------------------------------------------------------------------
# 4. Targeted probes: exact messages for every diagnostic class
# ---------------------------------------------------------------------------

VALID_PROBES = [
    # a syscall-shaped stub with the architecture clobber list
    ('syscall shape',
     'fn main() -> int { let mut ret: int = 231\n'
     ' asm!("syscall", in("rdi") 0, inout("rax") ret, clobber("rcx", "r11"), options(nomem))\n'
     ' return 0 }'),
    # full 64-bit names for int operands
    ('64-bit names',
     'fn main() -> int { let mut a: int = 1\n'
     ' asm!("nop", in("rax") a, inout("rbx") a, out("r9") a, options(nomem))\n'
     ' return 0 }'),
    # extended + pinned SSE registers (register variables)
    ('r10/xmm regvars',
     'fn main() -> int { let mut i: int = 1\n let mut f: float = 0.0\n'
     ' asm!("nop", inout("r10") i, in("xmm4") f, out("xmm1") f, clobber("r11", "xmm3"), options(nomem))\n'
     ' return 0 }'),
    # bool through a named register (int64_t register-variable carrier)
    ('bool named reg',
     'fn main() -> int { let mut b: bool = false\n'
     ' asm!("nop", out("r14") b, options(nomem))\n return 0 }'),
    # float with the bare reg class (SSE) and a memory operand
    ('float reg/mem classes',
     'fn main() -> int { let mut f: float = 1.0\n'
     ' asm!("nop", in(reg) f, in(mem) f, options(nomem))\n return 0 }'),
    # fixed letters track the physical register for overlap analysis
    ('fixed letters a/d',
     'fn main() -> int { let mut x: int = 0\n'
     ' asm!("mov {1}, {0}", in("a") x, out("d") x, options(nomem, preserves_flags))\n'
     ' return 0 }'),
]
REJECT_PROBES = [
    ('32-bit name for int', 'in("eax") x',
     "asm! register 'eax' is 32-bit; an int operand needs a 64-bit register — use 'rax'"),
    ('16-bit name for int', 'in("ax") x',
     "asm! register 'ax' is 16-bit; an int operand needs a 64-bit register — use 'rax'"),
    ('8-bit name for int', 'in("al") x',
     "asm! register 'al' is 8-bit; an int operand needs a 64-bit register — use 'rax'"),
    ('SSE name for int', 'out("xmm0") x',
     "asm! register 'xmm0' is an SSE register; an int operand needs a general-purpose register (rax, rbx, ..., r15)"),
    ('GP name for float', 'in("rbx") f',
     "asm! register 'rbx' is a general-purpose register; a float operand needs an SSE register — use reg (the SSE class) or an xmm0..xmm15 name"),
    ('GP class for float', 'in("r") f',
     "asm! constraint 'r' is a general-purpose register class; a float operand needs an SSE class — use reg or an xmm0..xmm15 name"),
    ('x87 class for float', 'in("t") f',
     "asm! constraint 't' is an x87/MMX register class; a float operand needs the SSE class — use reg or an xmm0..xmm15 name"),
    ('int immediate for float', 'in("i") f',
     "asm! constraint 'i' is an integer-immediate class; a float operand must go through a register — use reg"),
    ('SSE class for int', 'in("x") x',
     "asm! constraint 'x' is an SSE register class; an int operand needs a general-purpose class (r, q, a, b, c, d, S, D)"),
    ('stack pointer operand', 'in("rsp") x',
     "asm! register 'rsp' cannot be bound to an operand — the compiler owns the stack pointer"),
    ('frame pointer operand', 'in("rbp") x',
     "asm! register 'rbp' cannot be bound to an operand — the compiler owns the frame pointer"),
    ('clobber destroys input', 'in("rax") x, clobber("rax")',
     "asm! clobber 'rax' overlaps operand 0 (in \"rax\") — a clobbered register cannot also be bound to an operand"),
    ('clobber destroys fixed letter', 'in("a") x, clobber("rax")',
     "asm! clobber 'rax' overlaps operand 0 (in \"a\") — a clobbered register cannot also be bound to an operand"),
    ('two operands one register', 'in("rax") x, out("rax") x',
     "asm! operands 0 and 1 are both bound to 'rax' — each fixed register can be bound to at most one operand"),
    ('clobber alias overlap', 'in("rax") x, clobber("eax")',
     "asm! clobber 'eax' names a 32-bit sub-register — clobbers must name the full 64-bit register ('rax')"),
    ('clobber duplicate', 'clobber("rcx", "rcx")',
     "asm! clobbers 'rcx' and 'rcx' overlap (both are rcx)"),
    ('clobber stack pointer', 'clobber("rsp")',
     "asm! clobber 'rsp' is not allowed — the compiler owns the stack pointer"),
    ('clobber frame pointer', 'clobber("ebp")',
     "asm! clobber 'ebp' is not allowed — the compiler owns the frame pointer"),
    ('clobber sub-register', 'clobber("eax")',
     "asm! clobber 'eax' names a 32-bit sub-register — clobbers must name the full 64-bit register ('rax')"),
    ('clobber bad name', 'clobber("raxx")',
     "asm! clobber 'raxx' is not a valid x86-64 register name, 'cc' or 'memory'"),
    ('clobber cc reserved', 'in("rax") x, clobber("cc")',
     "asm! clobber 'cc' is not allowed — flag effects are controlled by options(preserves_flags), not the clobber list (the default clobber list already includes \"cc\")"),
    ('clobber memory reserved', 'clobber("memory")',
     "asm! clobber 'memory' is not allowed — memory effects are controlled by options(nomem), not the clobber list (the default clobber list already includes \"memory\")"),
    ('two clobber clauses', 'clobber("rcx"), clobber("r11")',
     "asm! clobber list appears more than once — merge all clobbers into a single clobber(...) clause"),
    ('non-string clobber', 'clobber(rcx)',
     "asm! clobber names must be string literals (a register name like \"rcx\", or \"cc\" / \"memory\")"),
]


def test_probes() -> None:
    section("4. Targeted probes (boot): valid forms accepted, diagnostics exact")
    for name, body in VALID_PROBES:
        p = write_tmp(".hls", body)
        r = boot("--check", p)
        check("accepts: %s" % name, r.returncode == 0,
              ((r.stdout or "") + (r.stderr or "")).strip()[:90])
    for name, fragment, needle in REJECT_PROBES:
        src = ('fn main() -> int { let mut x: int = 0\n'
               ' let mut f: float = 1.0\n'
               ' asm!("nop", ' + fragment + ', options(nomem))\n'
               ' return 0 }')
        # non-string clobber probe is a PARSE error: drop the options
        # tail so the paren shape still parses up to the error
        if name == 'non-string clobber':
            src = ('fn main() -> int { asm!("nop", clobber(rcx))\n'
                   ' return 0 }')
        if name == 'two clobber clauses':
            src = ('fn main() -> int { asm!("nop", clobber("rcx"), '
                   'clobber("r11"))\n return 0 }')
        p = write_tmp(".hls", src)
        r = boot("--check", p)
        out = (r.stdout or "") + (r.stderr or "")
        check("rejects: %s" % name,
              r.returncode == 1 and needle in out,
              out.strip().splitlines()[0][:110] if out.strip() else "no error")


# ---------------------------------------------------------------------------
# 5. Self-hosted emission (hlc) + native parity
# ---------------------------------------------------------------------------

def test_emission() -> None:
    section("5. Self-hosted emission (hlc) + native parity")
    hlc_src = os.path.join(REPO_ROOT, "src", "hlc.hls")
    d = tempfile.mkdtemp(prefix="hls_asmreg_emit_")
    out = os.path.join(d, "demo.c")
    r = boot(hlc_src, DEMO, out)
    check("hlc emits hosted C", r.returncode == 0 and os.path.isfile(out),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    src = open(out, encoding="utf-8", errors="replace").read()
    check("local register variable for r10", '__asm__("r10")' in src)
    check("local register variable for r11", '__asm__("r11")' in src)
    check("local register variable for r14", '__asm__("r14")' in src)
    check("local register variable for xmm0", '__asm__("xmm0")' in src)
    check("syscall stub pins rax", '"syscall" : "+a"' in src)
    check("explicit clobbers emitted", '"cc", "rcx", "r11"' in src)
    check("xmm clobber emitted", '"cc", "xmm3"' in src)
    check("default clobbers kept", '"cc", "memory"' in src)
    check("bool writeback converts", "!= 0" in src)
    binp = os.path.join(d, "demo.bin")
    if subprocess.run([GCC, "-O2", "-Werror", "-o", binp, out,
                       "-lm", "-pthread"], capture_output=True).returncode == 0:
        nat = subprocess.run([binp], capture_output=True, text=True,
                             timeout=60)
        check("native demo DEMO OK",
              nat.returncode == 0 and "DEMO OK" in (nat.stdout or ""),
              "rc=%d" % nat.returncode)
        check("xchg swapped via pinned regs",
              "a=222 b=111" in (nat.stdout or ""))
    else:
        check("native demo links (-Werror)", False, "gcc failed")
    # The no_std ok-test compiles hosted + freestanding, exit 0 both.
    out3 = os.path.join(d, "ok.c")
    r = boot(hlc_src, OKTEST, out3)
    check("hlc emits no_std ok C", r.returncode == 0 and os.path.isfile(out3))
    bin3 = os.path.join(d, "ok.bin")
    if subprocess.run([GCC, "-O2", "-Werror", "-o", bin3, out3,
                       "-lm", "-pthread"], capture_output=True).returncode == 0:
        nat = subprocess.run([bin3], capture_output=True, timeout=60)
        check("native ok-test exit 0", nat.returncode == 0,
              "rc=%d" % nat.returncode)
    else:
        check("native ok-test links", False, "gcc failed")
    fs_src = open(OKTEST, encoding="utf-8").read().replace(
        "\n#![no_std]\n", "\n#![freestanding]\n", 1)
    fs_p = write_tmp(".hls", fs_src)
    interp = boot(fs_p)
    out2 = os.path.join(d, "fs.c")
    r = boot(hlc_src, fs_p, out2)
    check("hlc emits freestanding C",
          r.returncode == 0 and os.path.isfile(out2),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    check("freestanding _start emitted",
          "void _start" in open(out2, encoding="utf-8",
                                errors="replace").read())
    bin2 = os.path.join(d, "fs.bin")
    if subprocess.run([GCC, "-O2", "-ffreestanding", "-nostdlib",
                       "-ffunction-sections", "-fno-stack-protector",
                       "-Wl,--gc-sections", "-o", bin2, out2],
                      capture_output=True).returncode == 0:
        nat = subprocess.run([bin2], capture_output=True, timeout=60)
        check("freestanding native == interpreter",
              nat.returncode == interp.returncode,
              "interp=%d nat=%d" % (interp.returncode, nat.returncode))
    else:
        check("freestanding binary links", False, "gcc failed")
    # Boot/hlc parity on the fail programs: the self-hosted checker
    # rejects them with the same Stage 83 messages.
    for path in FAILS:
        outf = os.path.join(d, "fail.c")
        r = boot(hlc_src, path, outf)
        txt = (r.stdout or "") + (r.stderr or "")
        check("hlc rejects %s" % os.path.basename(path),
              r.returncode != 0 and "asm!" in txt,
              txt.strip().splitlines()[0][:110] if txt.strip() else "no error")


# ---------------------------------------------------------------------------
# 6. hlfmt stability
# ---------------------------------------------------------------------------

def test_fmt() -> None:
    section("6. hlfmt stability over every Stage 83 file")
    files = [ASM, DEMO, OKTEST] + FAILS
    for f in files:
        r = subprocess.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                            "-c", f], capture_output=True, text=True,
                           cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean: %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""),
              (r.stdout or "").strip()[:80])


# ---------------------------------------------------------------------------
# 7. --audit: every core.asm function pure
# ---------------------------------------------------------------------------

def test_audit() -> None:
    section("7. --audit: every core.asm function pure (no uses)")
    r = boot("--audit", OKTEST)
    out = r.stdout or ""
    check("audit ran", r.returncode == 0)
    must_be_pure = [
        "asm64_gp_regs", "asm64_sse_regs", "asm_reg_width",
        "asm_reg_is_sse", "asm_reg_base", "asm_regs_overlap",
        "asm_valid_clobber", "asm_clobber_fault", "asm_operand_conflict",
        "asm_fault_name", "asm_fault_code", "asm_sysv_arg_reg",
        "asm_syscall_arg_reg", "asm_syscall_clobbers", "asm_sysv_ret_reg",
        "asm_sysv_fp_ret_reg", "asm_caller_saved", "asm_callee_saved",
        "asm_regs_to_save", "asm_scratch_reg", "asm_check_invariants",
    ]
    missing = [f for f in must_be_pure if f not in out]
    check("all asm fns in audit output", not missing,
          "missing: %s" % missing[:5])
    bad = []
    for line in out.splitlines():
        s = line.strip()
        for f in must_be_pure:
            if s.startswith(f + " "):
                if "(none - pure)" not in s and "(none)" not in s:
                    bad.append(s)
                break
    check("all asm fns declared pure", not bad,
          "impure: %s" % bad[:3])
    check("crate mode reported",
          "Crate mode:" in out and "#![no_std]" in out)


def main() -> int:
    test_resolution()
    test_parse()
    test_enforcement()
    test_probes()
    test_emission()
    test_fmt()
    test_audit()
    print()
    if check.failed:
        print("RESULT: %d FAIL" % check.failed)
        return 1
    print("RESULT: all sections OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
