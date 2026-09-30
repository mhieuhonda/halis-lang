#!/usr/bin/env python3
"""Stage 93 acceptance gate — target x86_64-unknown-none, the first
bare-metal triple.

Run with `make triple-x86_64-acceptance` (or
`python3 tests/triple_x86_64_acceptance.py`).

Eight sections:

  1. the CLI surface        — the triple names, and the two flag
                              combinations a bare-metal build refuses
                              (--target-feature: SIMD computes in FP
                              registers; --no-libc: the hosted no-libc
                              mode, not a freestanding image) — the
                              SAME words from both front-ends
  2. the integer-only rule  — eight fail programs (a float literal, a
                              parameter, a return, a struct field, an
                              enum payload, a let, a generic argument,
                              a list element) rejected by BOTH
                              compilers with the SAME words, and the
                              boundary that every one of them stays a
                              LEGAL hosted crate (they are
                              deliberately not in tests/fail/)
  3. the implication        — --target implies #![freestanding]: the
                              audits of a crate that never declared it
                              agree, line for line
  4. the emission shape     — the triple stamp in the C, the 3-header
                              freestanding TU, the naked _start with
                              the x86-64 raw-exit branch
  5. the image              — the end-to-end link: ELF64 x86-64,
                              entry 0x100000, _start the lowest symbol,
                              ZERO undefined symbols, and the probe
                              runs on the host with exit 42
  6. the orchestrator       — hlcross --target drives hlc --target
                              plus the freestanding link (and the
                              registry lists all three triples)
  7. the linker script      — targets/x86_64-unknown-none.ld: _start
                              kept and placed first, the metadata
                              sections, the boot-header keeps
  8. the tools              — hlfmt stable, hllint clean on the new
                              sources
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
TMP = tempfile.mkdtemp(prefix="_gate_triple93_", dir=os.path.join(ROOT, "tests"))

TRIPLE = "x86_64-unknown-none"
DEMO = "examples/bare_x86_64.hls"

# (file stem, needle in the diagnostic, what the probe pins)
FAIL_PROGRAMS = [
    ("fail_bare_float_literal", "float literals are not available",
     "a float literal in an expression"),
    ("fail_bare_float_param", "the type 'float' is not available",
     "a float parameter annotation"),
    ("fail_bare_float_ret", "the type 'float' is not available",
     "a float return annotation"),
    ("fail_bare_float_field", "the type 'list[float]' is not available",
     "a float struct field"),
    ("fail_bare_float_variant", "the type 'float' is not available",
     "a float enum payload"),
    ("fail_bare_float_let", "the type 'float' is not available",
     "a float let annotation"),
    ("fail_bare_float_listelem", "the type 'list[float]' is not available",
     "float as a generic list argument"),
    ("fail_bare_float_generic", "the type 'Cell[float]' is not available",
     "float as a generic struct argument"),
]


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def normalise(text):
    """Strip the front-end-specific prefix and the line suffix so the
    two compilers' diagnostics compare byte for byte (same convention
    as the Stage 86/91 gates)."""
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
    text = (r.stdout + r.stderr).strip()
    return r.returncode, text


def hlc_compile_target(src, out_c, with_target=True):
    cmd = ["./bin/hlc"]
    if with_target:
        cmd += ["--target", TRIPLE]
    cmd += [src, out_c]
    r = run(cmd)
    text = (r.stdout + r.stderr).strip()
    return r.returncode, text


def elf_header(path):
    """(ei_class, ei_data, e_machine, e_entry) of an ELF64 image."""
    with open(path, "rb") as f:
        blob = f.read(64)
    if blob[:4] != b"\x7fELF":
        return None
    ei_class, ei_data = blob[4], blob[5]
    e_type, e_machine = struct.unpack_from("<HH", blob, 16)
    e_entry = struct.unpack_from("<Q", blob, 24)[0]
    return (ei_class, ei_data, e_machine, e_entry)


# ---------------------------------------------------------------------------
print("=== 1. the CLI surface: the triples and the refused combinations ===")
# ---------------------------------------------------------------------------

BAD_TRIPLE_MSG = ("unknown bare-metal target 'armv7-unknown-none' "
                  "(expected x86_64-unknown-none | aarch64-unknown-none | "
                  "riscv64-unknown-none)")
b = run(["python3", "boot/boot.py", "--check", "--target",
         "armv7-unknown-none", DEMO])
h = run(["./bin/hlc", "--target", "armv7-unknown-none", DEMO,
         os.path.join(TMP, "never.c")])
# hlc's CLI diagnostics print to stdout (println); boot's to stderr.
b_text = (b.stdout + b.stderr).strip()
h_text = (h.stdout + h.stderr).strip()
if b.returncode == 0 or h.returncode == 0:
    bad("CLI: an unknown triple was accepted (boot rc=%d, hlc rc=%d)"
        % (b.returncode, h.returncode))
elif normalise(b_text) != normalise(h_text):
    bad("CLI: front-ends disagree on the unknown-triple words:\n"
        "    boot: %s\n    hlc:  %s" % (b_text, h_text))
elif BAD_TRIPLE_MSG not in b_text:
    bad("CLI: the unknown-triple diagnostic does not name the registry: %s"
        % b_text)
else:
    ok("CLI: an unknown triple is refused with the registry, both front-ends")

FEAT_MSG = ("--target-feature is not available on target %s "
            "(bare-metal targets are integer-only: SIMD intrinsics use "
            "floating-point registers)" % TRIPLE)
b = run(["python3", "boot/boot.py", "--check", "--target", TRIPLE,
         "--target-feature", "neon", DEMO])
h = run(["./bin/hlc", "--target", TRIPLE, "--target-feature", "neon",
         DEMO, os.path.join(TMP, "never.c")])
b_text = (b.stdout + b.stderr).strip()
h_text = (h.stdout + h.stderr).strip()
if b.returncode == 0 or h.returncode == 0:
    bad("CLI: --target-feature under --target was accepted")
elif normalise(b_text) != normalise(h_text):
    bad("CLI: front-ends disagree on the --target-feature words:\n"
        "    boot: %s\n    hlc:  %s" % (b_text, h_text))
elif FEAT_MSG not in b_text:
    bad("CLI: the --target-feature diagnostic is not the integer-only one: %s"
        % b_text)
else:
    ok("CLI: --target-feature refused on the triple, same words from both")

NOLIBC_MSG = ("--no-libc is not available on target %s "
              "(the bare-metal triples build freestanding images, not "
              "hosted no-libc ones)" % TRIPLE)
h = run(["./bin/hlc", "--target", TRIPLE, "--no-libc", DEMO,
         os.path.join(TMP, "never.c")])
h_text = (h.stdout + h.stderr).strip()
if h.returncode == 0:
    bad("CLI: --no-libc under --target was accepted")
elif NOLIBC_MSG not in h_text:
    bad("CLI: the --no-libc diagnostic is not the mode-conflict one: %s"
        % h_text)
else:
    ok("CLI: --no-libc refused on the triple (hosted mode vs freestanding)")

h = run(["./bin/hlc", "--target", TRIPLE, DEMO,
         os.path.join(TMP, "cli_ok.c")])
if h.returncode != 0:
    bad("CLI: the triple was refused on a legal program: %s" % h.stderr)
else:
    ok("CLI: the triple compiles a legal program (exit 0)")

# ---------------------------------------------------------------------------
print("=== 2. the integer-only rule: the fail programs, both front-ends ===")
# ---------------------------------------------------------------------------
for stem, needle, why in FAIL_PROGRAMS:
    src = "tests/bare/%s.hls" % stem
    brc, btxt = boot_check_target(src)
    hrc, htxt = hlc_compile_target(src, os.path.join(TMP, stem + ".c"))
    if brc == 0:
        bad("%s: boot accepted a float under --target (%s)" % (stem, why))
        continue
    if hrc == 0:
        bad("%s: hlc accepted a float under --target (%s)" % (stem, why))
        continue
    bmsg = normalise(btxt.splitlines()[0])
    hmsg = normalise(htxt.splitlines()[0])
    if bmsg != hmsg:
        bad("%s: boot and hlc disagree on the words:\n    boot: %s\n    hlc:  %s"
            % (stem, bmsg, hmsg))
        continue
    if needle not in bmsg:
        bad("%s: diagnostic does not name the construct ('%s'): %s"
            % (stem, needle, bmsg))
        continue
    ok("%s rejected identically by both front-ends (%s)" % (stem, why))

# The boundary: every fail program is a LEGAL hosted crate. They live in
# tests/bare/ (not tests/fail/) precisely because the rule is
# conditional on the triple.
legal = True
for stem, _needle, _why in FAIL_PROGRAMS:
    src = "tests/bare/%s.hls" % stem
    rrc, rtxt = boot_check_target(src, with_target=False)
    if rrc != 0:
        legal = False
        bad("%s: rejected WITHOUT --target — the rule is not "
            "triple-conditional: %s" % (stem, rtxt.splitlines()[0]))
if legal:
    ok("boundary: all %d probes compile as hosted crates without --target"
       % len(FAIL_PROGRAMS))

# ---------------------------------------------------------------------------
print("=== 3. the implication: --target implies #![freestanding] ===")
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
if b.returncode != 0 or h.returncode != 0:
    bad("imply: an audit failed (boot rc=%d, hlc rc=%d)"
        % (b.returncode, h.returncode))
elif len(b_lines) != 2 or len(h_lines) != 2:
    bad("imply: expected the crate-mode and target lines from both audits:\n"
        "    boot: %r\n    hlc:  %r" % (b_lines, h_lines))
elif b_lines != h_lines:
    bad("imply: the audits disagree:\n    boot: %r\n    hlc:  %r"
        % (b_lines, h_lines))
elif "Target: %s (bare-metal, integer-only)" % TRIPLE not in b_lines[1]:
    bad("imply: the target line is not stated: %r" % b_lines)
else:
    ok("imply: the audits agree — #![freestanding] implied, target stated")

# And the std boundary travels with the implication.
STD_IMP = """import "std.str"

fn main() -> int {
    return 0
}
"""
spath = os.path.join(TMP, "stdimp.hls")
with open(spath, "w") as fh:
    fh.write(STD_IMP)
brc, btxt = boot_check_target(spath)
if brc == 0:
    bad("imply: std imports survived the freestanding implication")
elif "the std module is disabled" not in btxt:
    bad("imply: the std rejection is not the freestanding one: %s"
        % btxt.splitlines()[0])
else:
    ok("imply: std imports are refused under the bare-metal triple")

# ---------------------------------------------------------------------------
print("=== 4. the emission shape: the stamp, the TU, the exit branch ===")
# ---------------------------------------------------------------------------
demo_c = os.path.join(TMP, "demo.c")
hrc, htxt = hlc_compile_target(DEMO, demo_c)
src = open(demo_c, encoding="utf-8", errors="replace").read() if hrc == 0 else ""
if hrc != 0:
    bad("shape: the demo failed to compile under --target: %s" % htxt)
else:
    inc = sorted(set(re.findall(r"#include\s*<([^>]+)>", src)))
    if inc != ["stdbool.h", "stddef.h", "stdint.h"]:
        bad("shape: the freestanding TU includes more than the 3 headers: %r"
            % inc)
    elif "target: %s (bare-metal, integer-only, freestanding)" % TRIPLE \
            not in src:
        bad("shape: the C does not carry the triple stamp")
    elif not re.search(r"void _start\(void\)", src):
        bad("shape: the naked _start is missing")
    elif "__builtin_trap" not in src or "exit(101)" in src:
        bad("shape: panics do not trap (or libc exit leaked in)")
    elif "movl $60, %eax" not in src or "syscall" not in src:
        bad("shape: the x86-64 raw-exit branch is missing from _start")
    else:
        ok("shape: 3-header TU, triple stamp, naked _start, x86-64 exit")

# ---------------------------------------------------------------------------
print("=== 5. the image: linked, inspected, and RUN on this host ===")
# ---------------------------------------------------------------------------
img = os.path.join(TMP, "bare_x86_64")
g = run(["gcc", "-O2", "-mgeneral-regs-only", "-ffreestanding", "-nostdlib",
         "-nostartfiles", "-fno-stack-protector", "-fno-pie", "-no-pie",
         "-ffunction-sections", "-Wl,--gc-sections,-T,targets/%s.ld" % TRIPLE,
         "-o", img, demo_c])
if g.returncode != 0:
    bad("image: the link failed: %s" % g.stderr.strip()[:400])
else:
    hdr = elf_header(img)
    if hdr is None:
        bad("image: not an ELF file")
    else:
        ei_class, ei_data, e_machine, e_entry = hdr
        if ei_class != 2 or ei_data != 1 or e_machine != 62:
            bad("image: wrong ELF identity (class=%d data=%d machine=%d — "
                "want 64-bit LE EM_X86_64=62)" % (ei_class, ei_data, e_machine))
        elif e_entry != 0x100000:
            bad("image: entry 0x%x is not the script's base 0x100000"
                % e_entry)
        else:
            ok("image: ELF64 LE, EM_X86_64, entry at the 1 MiB base")
    nm = run(["nm", "-n", img])
    if nm.returncode != 0:
        bad("image: nm failed")
    else:
        syms = [ln.split()[-1] for ln in nm.stdout.splitlines() if ln.strip()]
        if "_start" not in syms:
            bad("image: no _start symbol")
        elif syms[0] != "_start":
            bad("image: the lowest symbol is %s, not _start "
                "(the base must be the entry)" % syms[0])
        else:
            ok("image: _start is the lowest symbol (the base is the entry)")
    undef = run(["nm", "-u", img])
    if undef.returncode == 0 and undef.stdout.strip():
        bad("image: undefined symbols survive: %s"
            % undef.stdout.strip().splitlines()[:3])
    else:
        ok("image: zero undefined symbols (no libc, nothing external)")
    rr = run([img], timeout=60)
    if rr.returncode != 42:
        bad("image: the probe exited %d, not 42" % rr.returncode)
    else:
        ok("image: the bare-metal probe runs on this host and exits 42")

# ---------------------------------------------------------------------------
print("=== 6. the orchestrator: hlcross drives the whole pipeline ===")
# ---------------------------------------------------------------------------
out_bin = os.path.join(TMP, "cross_bin")
keep_c = os.path.join(TMP, "cross.c")
cr = run(["python3", "tools/hlcross.py", DEMO, out_bin,
          "--target", TRIPLE, "--keep-c", keep_c])
if cr.returncode != 0:
    bad("orchestrator: hlcross failed (rc=%d): %s"
        % (cr.returncode, (cr.stdout + cr.stderr).strip()[:400]))
else:
    csrc = open(keep_c, encoding="utf-8", errors="replace").read()
    rr = run([out_bin], timeout=60)
    if "target: %s (bare-metal, integer-only, freestanding)" % TRIPLE \
            not in csrc:
        bad("orchestrator: the kept C lost the triple stamp (hlc --target "
            "was not passed through)")
    elif rr.returncode != 42:
        bad("orchestrator: the hlcross image exited %d, not 42"
            % rr.returncode)
    else:
        ok("orchestrator: hlcross --target built a working image "
           "(hlc --target + freestanding link)")

lst = run(["python3", "tools/hlcross.py", "--list-targets"])
# Stage 93 names its own triple (and the RISC-V one that has been in the
# registry since Stage 26); the AArch64 entry arrives with Stage 94, the
# full-trio check with Stage 95's gate.
registered = all(t in lst.stdout for t in
                 ("x86_64-unknown-none", "riscv64-unknown-none"))
if lst.returncode != 0 or not registered:
    bad("orchestrator: --list-targets does not name the bare-metal triples")
else:
    ok("orchestrator: the registry names the bare-metal triples")

# ---------------------------------------------------------------------------
print("=== 7. the linker script: the shape of the image ===")
# ---------------------------------------------------------------------------
with open("targets/%s.ld" % TRIPLE, encoding="utf-8") as fh:
    script = fh.read()
checks = [
    ("ENTRY(_start)", "the entry is _start"),
    (". = 0x100000;", "the base is 1 MiB"),
    ("KEEP(*(.text._start))", "_start is kept and placed first"),
    ("KEEP(*(.multiboot_header))", "the Multiboot2 header survives "
     "--gc-sections (Stage 85 interop)"),
    ("KEEP(*(.limine_requests))", "the Limine requests survive "
     "--gc-sections (Stage 85 interop)"),
    ("__halis_metadata_start", "the Stage 84 metadata section is bounded"),
    ("__halis_sections_start", "the Stage 84 custom-section region is "
     "bounded"),
    ("*(.eh_frame)", "eh_frame is discarded (no unwind tables in an image "
     "with no unwinder)"),
    ("NOLOAD", "bss is NOLOAD (a bare image has no loader to zero it "
     "page by page)"),
]
bad_script = False
for needle, why in checks:
    if needle not in script:
        bad("script: %s — missing '%s'" % (why, needle))
        bad_script = True
if not bad_script:
    if script.find("KEEP(*(.text._start))") > script.find("*(.text .text.*)"):
        bad("script: .text._start must be placed BEFORE the general .text "
            "glob")
    else:
        ok("script: entry, base, _start-first, metadata, boot keeps, "
           "discards — all in place")

# ---------------------------------------------------------------------------
print("=== 8. the tools: hlfmt stable, hllint clean ===")
# ---------------------------------------------------------------------------
fmt1 = run(["python3", "tools/hlfmt.py", DEMO])
fmt2 = run(["python3", "tools/hlfmt.py", DEMO])
if fmt1.returncode == 0 and fmt2.returncode == 0 \
        and fmt1.stdout == fmt2.stdout and fmt1.stdout.strip():
    ok("tools: hlfmt stable on the demo")
else:
    bad("tools: hlfmt unstable or empty on the demo")

lint_ok = True
for f in (DEMO, "tests/bare/fail_bare_float_literal.hls"):
    lint = run(["python3", "tools/hllint.py", f])
    if lint.returncode != 0:
        lint_ok = False
        bad("tools: hllint findings on %s:\n%s"
            % (f, (lint.stdout + lint.stderr).strip()[:300]))
if lint_ok:
    ok("tools: hllint clean on the demo and the probes")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 93 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
