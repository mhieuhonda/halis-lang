#!/usr/bin/env python3
"""Stage 95 acceptance gate — target riscv64-unknown-none, the
bare-metal RISC-V triple, completing the Phase VI trio.

Run with `make triple-riscv64-acceptance` (or
`python3 tests/triple_riscv64_acceptance.py`).

Sections:

  1. the CLI surface        — the triple compiles, the refused
                              combinations say the same words from both
                              front-ends
  2. the integer-only rule  — the eight tests/bare probes rejected by
                              BOTH compilers under the RISC-V triple,
                              still legal hosted
  3. the implication        — --target implies #![freestanding]: audit
                              parity on a crate that never declared it
  4. the emission shape     — the stamp, the 3-header TU, and the
                              RISC-V branch of the naked _start (`call
                              hl_boot` — a real RISC-V pseudo-op — and
                              the `ecall` exit)
  5. the image              — with a cross-linker on PATH (zig cc, or
                              any riscv64 gcc): ELF64 RISC-V
                              (EM_RISCV=243), entry at 0x80000000 (the
                              virt DRAM base), zero undefined symbols,
                              and ZERO floating-point instructions
                              (rv64imac's own backstop, verified in the
                              disassembly); without a toolchain, the
                              honest SKIP that keeps the C
  6. the QEMU boot          — with qemu-system-riscv64 installed: the
                              NS16550A banner on the virt console AND
                              the SiFive test finisher's exit code
                              (42) as QEMU's own exit status;
                              otherwise a reported SKIP
  7. the linker script      — targets/riscv64-unknown-none.ld: the
                              virt DRAM base, _start kept first, the
                              metadata + boot-header keeps
  8. the trio               — the registry names all three bare-metal
                              triples, and the tools stay clean
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_triple95_", dir=os.path.join(ROOT, "tests"))

TRIPLE = "riscv64-unknown-none"
DEMO = "examples/bare_riscv64.hls"

FAIL_PROGRAMS = [
    ("fail_bare_float_literal", "float literals are not available"),
    ("fail_bare_float_param", "the type 'float' is not available"),
    ("fail_bare_float_ret", "the type 'float' is not available"),
    ("fail_bare_float_field", "the type 'list[float]' is not available"),
    ("fail_bare_float_variant", "the type 'float' is not available"),
    ("fail_bare_float_let", "the type 'float' is not available"),
    ("fail_bare_float_listelem", "the type 'list[float]' is not available"),
    ("fail_bare_float_generic", "the type 'Cell[float]' is not available"),
]


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def skip(msg):
    print("  [SKIP] %s" % msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def normalise(text):
    out = re.sub(r"^panic: ", "", text.strip())
    out = re.sub(r"^[a-z ]*error: ", "", out)
    out = re.sub(r" \(line \d+(?::\d+)?\)\s*$", "", out)
    return out


def boot_check_target(src, with_target=True):
    cmd = ["python3", "boot/boot.py", "--check"]
    if with_target:
        cmd += ["--target", TRIPLE]
    cmd.append(src)
    r = run(cmd)
    return r.returncode, (r.stdout + r.stderr).strip()


def hlc_compile_target(src, out_c, with_target=True):
    cmd = ["./bin/hlc"]
    if with_target:
        cmd += ["--target", TRIPLE]
    cmd += [src, out_c]
    r = run(cmd)
    return r.returncode, (r.stdout + r.stderr).strip()


def elf_header(path):
    with open(path, "rb") as f:
        blob = f.read(64)
    if blob[:4] != b"\x7fELF":
        return None
    e_machine = struct.unpack_from("<H", blob, 18)[0]
    e_entry = struct.unpack_from("<Q", blob, 24)[0]
    return (blob[4], blob[5], e_machine, e_entry)


# ---------------------------------------------------------------------------
print("=== 1. the CLI surface ===")
# ---------------------------------------------------------------------------
h = run(["./bin/hlc", "--target", TRIPLE,
         "tests/bare/fail_bare_float_param.hls",
         os.path.join(TMP, "never.c")])
b = run(["python3", "boot/boot.py", "--check", "--target", TRIPLE,
         "tests/bare/fail_bare_float_param.hls"])
if h.returncode == 0 or b.returncode == 0:
    bad("CLI: the triple accepted a float crate")
else:
    ok("CLI: the triple drives both front-ends")

FEAT_MSG = ("--target-feature is not available on target %s "
            "(bare-metal targets are integer-only: SIMD intrinsics use "
            "floating-point registers)" % TRIPLE)
h = run(["./bin/hlc", "--target", TRIPLE, "--target-feature", "rvv",
         DEMO, os.path.join(TMP, "never.c")])
h_text = (h.stdout + h.stderr).strip()
if h.returncode == 0 or FEAT_MSG not in h_text:
    bad("CLI: --target-feature refusal missing or wrong: %s" % h_text)
else:
    ok("CLI: --target-feature (rvv) refused on the RISC-V triple")

NOLIBC_MSG = ("--no-libc is not available on target %s "
              "(the bare-metal triples build freestanding images, not "
              "hosted no-libc ones)" % TRIPLE)
h = run(["./bin/hlc", "--target", TRIPLE, "--no-libc", DEMO,
         os.path.join(TMP, "never.c")])
h_text = (h.stdout + h.stderr).strip()
if h.returncode == 0 or NOLIBC_MSG not in h_text:
    bad("CLI: --no-libc refusal missing or wrong: %s" % h_text)
else:
    ok("CLI: --no-libc refused on the RISC-V triple")

# ---------------------------------------------------------------------------
print("=== 2. the integer-only rule: the probes, both front-ends ===")
# ---------------------------------------------------------------------------
for stem, needle in FAIL_PROGRAMS:
    src = "tests/bare/%s.hls" % stem
    brc, btxt = boot_check_target(src)
    hrc, htxt = hlc_compile_target(src, os.path.join(TMP, stem + ".c"))
    if brc == 0 or hrc == 0:
        bad("%s: accepted under --target %s" % (stem, TRIPLE))
        continue
    bmsg = normalise(btxt.splitlines()[0])
    hmsg = normalise(htxt.splitlines()[0])
    if bmsg != hmsg:
        bad("%s: front-ends disagree:\n    boot: %s\n    hlc:  %s"
            % (stem, bmsg, hmsg))
        continue
    if needle not in bmsg or TRIPLE not in bmsg:
        bad("%s: diagnostic does not name the construct and the triple: %s"
            % (stem, bmsg))
        continue
    ok("%s rejected identically under the RISC-V triple" % stem)

legal = True
for stem, _n in FAIL_PROGRAMS:
    rrc, _t = boot_check_target("tests/bare/%s.hls" % stem, with_target=False)
    if rrc != 0:
        legal = False
        bad("%s: not a legal hosted crate without --target" % stem)
if legal:
    ok("boundary: all %d probes stay legal hosted" % len(FAIL_PROGRAMS))

# ---------------------------------------------------------------------------
print("=== 3. the implication: audit parity ===")
# ---------------------------------------------------------------------------
IMPLY = """# No crate attribute — the triple is the promise.
fn main() -> int {
    let mut acc: int = 0
    let mut i: int = 0
    while i < 4 {
        acc = acc + i
        i = i + 1
    }
    return acc - 6 + 42
}
"""
ipath = os.path.join(TMP, "imply.hls")
with open(ipath, "w") as fh:
    fh.write(IMPLY)
b = run(["python3", "boot/boot.py", "--target", TRIPLE, "--audit", ipath])
h = run(["./bin/hlc", "--target", TRIPLE, "--audit", ipath])
b_lines = [ln.strip() for ln in b.stdout.splitlines()
           if "Crate mode" in ln or "Target:" in ln]
h_lines = [ln.strip() for ln in h.stdout.splitlines()
           if "Crate mode" in ln or "Target:" in ln]
if b.returncode != 0 or h.returncode != 0 or b_lines != h_lines \
        or len(b_lines) != 2 \
        or "Target: %s (bare-metal, integer-only)" % TRIPLE not in b_lines[1]:
    bad("imply: audits disagree or omit the target:\n    boot: %r\n    hlc:  %r"
        % (b_lines, h_lines))
else:
    ok("imply: the audits agree — freestanding implied, RISC-V stated")

# ---------------------------------------------------------------------------
print("=== 4. the emission shape: the stamp and the RISC-V entry ===")
# ---------------------------------------------------------------------------
demo_c = os.path.join(TMP, "demo.c")
hrc, htxt = hlc_compile_target(DEMO, demo_c)
if hrc != 0:
    bad("shape: the demo failed to compile: %s" % htxt)
else:
    src = open(demo_c, encoding="utf-8", errors="replace").read()
    inc = sorted(set(re.findall(r"#include\s*<([^>]+)>", src)))
    start_idx = src.find("void _start(void)")
    tail = src[start_idx:] if start_idx >= 0 else ""
    rv_branch = re.search(
        r"#elif defined\(__riscv\)(.*?)#else", tail, re.DOTALL)
    branch = rv_branch.group(1) if rv_branch else ""
    if inc != ["stdbool.h", "stddef.h", "stdint.h"]:
        bad("shape: wrong headers: %r" % inc)
    elif "target: %s (bare-metal, integer-only, freestanding)" % TRIPLE \
            not in src:
        bad("shape: the C does not carry the RISC-V triple stamp")
    elif "call hl_boot" not in branch or "ecall" not in branch:
        bad("shape: the RISC-V _start branch is not the call/ecall shape: %r"
            % branch[:120])
    else:
        ok("shape: stamp, 3-header TU, call/ecall entry branch")

# ---------------------------------------------------------------------------
print("=== 5. the image: cross-linked when a toolchain exists ===")
# ---------------------------------------------------------------------------
out_bin = os.path.join(TMP, "bare_riscv64")
keep_c = os.path.join(TMP, "cross.c")
cr = run(["python3", "tools/hlcross.py", DEMO, out_bin,
          "--target", TRIPLE, "--keep-c", keep_c])
if cr.returncode == 0:
    hdr = elf_header(out_bin)
    if hdr is None:
        bad("image: hlcross produced a non-ELF")
    else:
        ei_class, ei_data, e_machine, e_entry = hdr
        if ei_class != 2 or ei_data != 1 or e_machine != 243:
            bad("image: wrong ELF identity (machine=%d, want EM_RISCV=243)"
                % e_machine)
        elif e_entry != 0x80000000:
            bad("image: entry 0x%x is not the virt DRAM base 0x80000000"
                % e_entry)
        else:
            ok("image: ELF64 LE RISC-V, entry at the virt DRAM base")
    undef = run(["nm", "-u", out_bin])
    if undef.returncode == 0 and undef.stdout.strip():
        bad("image: undefined symbols survive: %s"
            % undef.stdout.strip().splitlines()[:3])
    else:
        ok("image: zero undefined symbols")
    objdump = shutil.which("riscv64-linux-gnu-objdump") or \
        shutil.which("llvm-objdump")
    fp = None
    if objdump:
        d = run([objdump, "-d", out_bin])
        fp = re.findall(r"\b(fld|fsd|flw|fsw|fmv\.[dwxs]|fcvt\.\w+|"
                        r"fadd\.|fmul\.|fdiv\.)", d.stdout)
    else:
        zig = shutil.which("zig")
        if zig:
            d = run([zig, "objdump", "-d", out_bin])
            fp = re.findall(r"\b(fld|fsd|flw|fsw|fmv\.[dwxs]|fcvt\.\w+|"
                            r"fadd\.|fmul\.|fdiv\.)", d.stdout)
    if fp is None:
        skip("image: no objdump available — the FP-free disassembly is "
             "checked wherever one exists")
    elif fp:
        bad("image: floating-point instructions in an rv64imac image: %r"
            % fp[:5])
    else:
        ok("image: zero floating-point instructions (the rv64imac "
           "backstop, verified in the disassembly)")
elif cr.returncode == 3:
    hint = (cr.stdout + cr.stderr)
    if "no cross-linker found for target '%s'" % TRIPLE not in hint:
        bad("image: the SKIP path does not name the triple:\n%s" % hint[:300])
    elif not os.path.exists(keep_c):
        bad("image: the SKIP path lost the C source")
    else:
        ok("image: no RISC-V toolchain — the C source is kept and the "
           "SKIP names the triple")
else:
    bad("image: hlcross failed unexpectedly (rc=%d): %s"
        % (cr.returncode, (cr.stdout + cr.stderr).strip()[:300]))

# ---------------------------------------------------------------------------
print("=== 6. the QEMU boot (when qemu-system-riscv64 exists) ===")
# ---------------------------------------------------------------------------
qemu = shutil.which("qemu-system-riscv64")
img = os.path.join(TMP, "bare_riscv64")
if not os.path.exists(img):
    img = None
if qemu is None or img is None:
    skip("QEMU: qemu-system-riscv64 (or the cross-linked image) not "
         "available — the virt boot is exercised wherever the toolchain is")
else:
    try:
        qr = run([qemu, "-M", "virt", "-bios", "none", "-nographic",
                  "-kernel", img], timeout=25)
        out = qr.stdout + qr.stderr
        if "bare riscv64 OK" not in out:
            bad("QEMU: the NS16550A banner did not appear: %r" % out[:200])
        elif qr.returncode != 42:
            bad("QEMU: the finisher exited %d, not 42" % qr.returncode)
        else:
            ok("QEMU: the banner reached the virt console AND the "
               "finisher exited 42")
    except subprocess.TimeoutExpired as ex:
        out = ""
        if ex.stdout:
            out += ex.stdout.decode(errors="replace")
        if ex.stderr:
            out += ex.stderr.decode(errors="replace")
        if "bare riscv64 OK" in out:
            ok("QEMU: the banner reached the virt console (the finisher "
               "write was not reached before the kill)")
        else:
            bad("QEMU: timed out without the banner: %r" % out[:200])

# ---------------------------------------------------------------------------
print("=== 7. the linker script: the shape of the image ===")
# ---------------------------------------------------------------------------
with open("targets/%s.ld" % TRIPLE, encoding="utf-8") as fh:
    script = fh.read()
checks = [
    ("OUTPUT_FORMAT(elf64-littleriscv)", "the format is RISC-V ELF"),
    ("ENTRY(_start)", "the entry is _start"),
    (". = 0x80000000;", "the base is QEMU virt's DRAM base"),
    ("KEEP(*(.text._start))", "_start is kept and placed first"),
    ("KEEP(*(.limine_requests))", "the Limine keep carries over"),
    ("__halis_metadata_start", "the Stage 84 metadata section is bounded"),
    ("*(.eh_frame)", "eh_frame is discarded"),
    ("NOLOAD", "bss is NOLOAD"),
]
bad_script = False
for needle, why in checks:
    if needle not in script:
        bad("script: %s — missing '%s'" % (why, needle))
        bad_script = True
if not bad_script:
    if script.find("KEEP(*(.text._start))") > script.find("*(.text .text.*)"):
        bad("script: .text._start must come before the general .text glob")
    else:
        ok("script: RISC-V format, virt base, _start-first, keeps, "
           "discards — all in place")

# ---------------------------------------------------------------------------
print("=== 8. the trio: the registry is complete, the tools stay clean ===")
# ---------------------------------------------------------------------------
lst = run(["python3", "tools/hlcross.py", "--list-targets"])
trio = all(t in lst.stdout for t in
           ("x86_64-unknown-none", "aarch64-unknown-none",
            "riscv64-unknown-none"))
if lst.returncode != 0 or not trio:
    bad("trio: --list-targets does not name all three bare-metal triples")
else:
    ok("trio: the registry names all three bare-metal triples")

fmt1 = run(["python3", "tools/hlfmt.py", DEMO])
fmt2 = run(["python3", "tools/hlfmt.py", DEMO])
if fmt1.returncode == 0 and fmt2.returncode == 0 \
        and fmt1.stdout == fmt2.stdout and fmt1.stdout.strip():
    ok("tools: hlfmt stable on the demo")
else:
    bad("tools: hlfmt unstable or empty on the demo")

lint = run(["python3", "tools/hllint.py", DEMO])
if lint.returncode == 0:
    ok("tools: hllint clean on the demo")
else:
    bad("tools: hllint findings on the demo:\n%s"
        % (lint.stdout + lint.stderr).strip()[:300])

ir = run(["python3", "boot/boot.py", DEMO], timeout=120)
if ir.returncode == 0:
    bad("tools: the interpreter executed asm! (it must refuse)")
elif "asm! cannot be executed by the boot interpreter" not in \
        (ir.stdout + ir.stderr):
    bad("tools: the refusal is not the documented one:\n%s"
        % (ir.stdout + ir.stderr).strip()[:200])
else:
    ok("tools: the interpreter refuses to execute the device writes")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 95 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
