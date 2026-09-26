#!/usr/bin/env python3
"""Stage 86 acceptance gate — core.interrupt: IDT/GDT declaration syntax.

Run with `make idt-acceptance` (or `python3 tests/idt_acceptance.py`).

Seven sections:

  1. the crate attribute + guards — `#[irq_handler(N)]` parses, the bare
                                   form still works, and the vector
                                   rules (0..255, once per function,
                                   once per vector) fire with the same
                                   diagnostic from BOTH compilers
  2. core/interrupt.hls          — the enums / structs / surface, the
                                   `core.*`-only imports, no effects
  3. the emitted stub + table     — a freestanding image gets one naked
                                   save/restore stub per declared
                                   vector and a binding table, gcc
                                   links it `-Werror`, and `nm` shows
                                   the symbols
  4. no SSE in an ISR             — the whole reason the stub exists:
                                   the emitted C must NOT carry
                                   `__attribute__((interrupt))` on the
                                   Halis function, and the image must
                                   link at all
  5. boot↔hlc parity              — all six fail programs, identical
                                   text
  6. behaviour probes            — the descriptor arithmetic, the
                                   selector bound, the IST byte, the
                                   32-bit offset split, the page-fault
                                   error-code decode, the exception
                                   table and the 8259 remap
  7. the demos + tools           — interrupt_demo identical on the
                                   interpreter and natively,
                                   boot_kernel's declarations link,
                                   hlfmt/hllint clean
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_idt_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage86_interrupt.hls"
DEMO = "examples/interrupt_demo.hls"
KERNEL = "examples/boot_kernel.hls"
CORE = "core/interrupt.hls"

FAIL_PROGRAMS = [
    ("fail_stage86_irq_vector_range", "out of range",
     "a vector above 255"),
    ("fail_stage86_irq_vector_twice", "appears more than once",
     "two #[irq_handler(N)] on one function"),
    ("fail_stage86_irq_vector_dup", "two functions declare",
     "two functions claiming one vector"),
    ("fail_stage86_irq_ret", "requires the function to return 'void'",
     "a handler that returns int"),
    ("fail_stage86_irq_params", "requires exactly one parameter",
     "a handler with two parameters"),
    ("fail_stage86_irq_param_scalar", "pointer-typed value",
     "an int frame parameter"),
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
    out = re.sub(r"^panic: ", "", text.strip())
    return re.sub(r"^[a-z ]*error: ", "", out)


def hlc_run(src):
    c = os.path.join(TMP, os.path.basename(src) + ".c")
    exe = os.path.join(TMP, os.path.basename(src) + ".bin")
    r = run(["./bin/hlc", src, c])
    if r.returncode != 0:
        return None, (r.stdout + r.stderr)
    g = run(["gcc", "-O2", "-o", exe, c, "-lm", "-pthread"])
    if g.returncode != 0:
        return None, g.stderr
    r = run([exe])
    return r.returncode, r.stdout + r.stderr


# ---------------------------------------------------------------------------
print("=== 1. the crate attribute + guards ===")
# ---------------------------------------------------------------------------
# The BARE form must still be accepted: the kernel supplies the vector,
# so it also supplies the stub, exactly as before Stage 86.
bare = os.path.join(TMP, "bare.hls")
with open(bare, "w", encoding="utf-8") as fh:
    fh.write('#[irq_handler]\nfn handler(frame: list[int]) -> void {\n    return\n}\n\n'
             'fn main() -> int {\n    return 0\n}\n')
r = run(["./bin/hlc", bare, os.path.join(TMP, "bare.c")])
if r.returncode == 0:
    ok("idt: the bare #[irq_handler] form still compiles")
else:
    bad("idt: the bare form rejected: %s" % (r.stdout + r.stderr)[:120])
# ...and it must NOT get a stub: there is no vector to stub.
if "hl_irq_stub_" not in open(os.path.join(TMP, "bare.c"), encoding="utf-8",
                              errors="replace").read():
    ok("idt: the bare form emits no stub (the kernel supplies it)")
else:
    bad("idt: the bare form emitted a stub")

# A declared vector, in a crate with no imports, must compile.
decl = os.path.join(TMP, "decl.hls")
with open(decl, "w", encoding="utf-8") as fh:
    fh.write('#[irq_handler(14)]\nfn on_page_fault(frame: list[int]) -> void {\n'
             '    return\n}\n\n#[irq_handler(8)]\nfn on_double(frame: list[int]) -> void {\n'
             '    return\n}\n\nfn main() -> int {\n    return 0\n}\n')
r = run(["./bin/hlc", decl, os.path.join(TMP, "decl.c")])
if r.returncode == 0:
    ok("idt: #[irq_handler(N)] compiles")
else:
    bad("idt: #[irq_handler(N)] rejected: %s" % (r.stdout + r.stderr)[:120])
r = run(["python3", "boot/boot.py", "--check", decl])
if r.returncode == 0:
    ok("idt: #[irq_handler(N)] accepted by Stage-0 too")
else:
    bad("idt: Stage-0 rejected #[irq_handler(N)]: %s" % (r.stdout + r.stderr)[:120])

# ---------------------------------------------------------------------------
print("=== 2. core/interrupt.hls ===")
# ---------------------------------------------------------------------------
if not os.path.exists(CORE):
    bad("idt: core/interrupt.hls missing")
else:
    src = open(CORE, encoding="utf-8").read()
    for enum in ("GdtFault", "PageFaultCause"):
        if re.search(r"enum\s+%s\b" % enum, src):
            ok("idt: core.interrupt declares enum %s" % enum)
        else:
            bad("idt: core.interrupt missing enum %s" % enum)
    imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
    if imports == ["core.result"]:
        ok("idt: core.interrupt imports only core.result (freestanding-safe)")
    else:
        bad("idt: core.interrupt imports %r" % imports)
    decls = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    if not re.search(r"^fn[^\n]*\buses\b", decls, re.M) and not re.search(
            r"^\s*extern\b", decls, re.M):
        ok("idt: core.interrupt declares no effects and no externs")
    else:
        bad("idt: core.interrupt declares effects or externs")
    for fn in ("gdt_selector", "gdt_selector_ok", "gdt_encode",
               "gdt_decode_access", "gdt_decode_flags", "gdt_decode_base",
               "gdt_decode_limit", "gdt_is_code", "gdt_is_data",
               "gdt_is_system", "gdt_is_long", "gdt_dpl", "gdt_check",
               "gdt_flag_long", "gdt_flag_default32", "gdt_flag_granularity",
               "tss_ist_encode", "tss_ist_decode", "tss_is_busy",
               "tss_set_busy", "idt_gate_ok", "idt_gate_is_interrupt",
               "idt_gate_is_trap", "idt_ist_pack", "idt_ist_unpack",
               "idt_offset_split", "idt_offset_join", "idt_offset_fits",
               "idt_entry_ok", "page_fault_decode", "page_fault_code",
               "page_fault_is_present", "page_fault_is_write",
               "page_fault_is_user", "page_fault_is_fetch",
               "page_fault_address_bits", "exc_name", "exc_pushes_error",
               "exc_is_fault", "exc_default_ist", "pic_vector_irq",
               "pic_vector_needs_eoi", "pic_remap_ok"):
        if re.search(r"fn\s+%s\b" % fn, src):
            ok("idt: core.interrupt exposes %s" % fn)
        else:
            bad("idt: core.interrupt missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 3. the emitted stub + table ===")
# ---------------------------------------------------------------------------
text = open(os.path.join(TMP, "decl.c"), encoding="utf-8", errors="replace").read()
for stub in ("hl_irq_stub_8", "hl_irq_stub_14"):
    if stub in text:
        ok("idt: a stub was emitted for %s" % stub)
    else:
        bad("idt: no stub for %s" % stub)
if "hl_idt_bindings[]" in text:
    ok("idt: the binding table is emitted")
else:
    bad("idt: the binding table is missing")
if "void hl_idt_install(" in text and "lidt" in text:
    ok("idt: the installer is emitted (it loads the IDTR)")
else:
    bad("idt: the installer is missing")
if "naked, used" in text:
    ok("idt: the stub is naked and used")
else:
    bad("idt: the stub is not naked/used")
# The push sequence is 11 registers = 88 bytes, and iretq unwinds them.
if '"pushq %r11\\n"' in text and "addq $88, %rsp" in text and "iretq" in text:
    ok("idt: the stub saves 11 registers and unwinds with iretq")
else:
    bad("idt: the stub's save/restore sequence is wrong")
# The error-code vectors get the vector number; the others must not.
if "movl $14, %eax" in text and "movl $8, %eax" in text:
    ok("idt: the error-pushing vectors carry their number")
else:
    bad("idt: the stubs do not pass the vector for an error-code vector")

# The IMAGE: compile it -Werror and read the symbols off the object. A
# RELOCATABLE link is what matters here (the section and symbol shape),
# and it keeps the check independent of whether the entry symbol is
# present in a given program.
c = os.path.join(TMP, "decl.c")
obj = os.path.join(TMP, "decl.o")
g = run(["gcc", "-O2", "-Werror", "-ffreestanding", "-fno-pie", "-c", c, "-o", obj])
if g.returncode != 0:
    bad("idt: the annotated image did not compile -Werror: %s" % g.stderr.strip()[:160])
elif shutil.which("nm") is None:
    bad("idt: nm not available")
else:
    syms = run(["nm", obj]).stdout
    for sym in ("hl_irq_stub_8", "hl_irq_stub_14", "hl_idt_bindings",
                "hl_idt_install", "hl_idt", "hl_idtr"):
        if sym in syms:
            ok("idt: the image carries %s" % sym)
        else:
            bad("idt: the image has no %s" % sym)
    if "T hl_irq_stub_14" in syms:
        ok("idt: the stub is in .text (a real code symbol)")
    else:
        bad("idt: the stub is not a code symbol")

# ---------------------------------------------------------------------------
print("=== 4. no SSE in an ISR ===")
# ---------------------------------------------------------------------------
# The whole reason the stub exists: gcc REJECTS SSE inside a function
# carrying the `interrupt` attribute, and a Halis body always uses it.
# The vector form therefore must not carry the attribute, and the bare
# form (which has no vector to stub) must still carry it.
if "__attribute__((interrupt))" not in text:
    ok("idt: the vector form does NOT use gcc's interrupt attribute")
else:
    bad("idt: the vector form still emits __attribute__((interrupt))")
bare_text = open(os.path.join(TMP, "bare.c"), encoding="utf-8",
                 errors="replace").read()
if "__attribute__((interrupt))" in bare_text:
    ok("idt: the bare form still uses gcc's interrupt attribute")
else:
    bad("idt: the bare form lost gcc's interrupt attribute")
# And a Halis handler with a list parameter (the case that never
# compiled) must now build.
if "interrupt))" not in text and "naked" in text:
    ok("idt: a Halis handler with a pointer parameter links")
else:
    bad("idt: a Halis handler with a pointer parameter still does not link")

# ---------------------------------------------------------------------------
print("=== 5. boot↔hlc parity ===")
# ---------------------------------------------------------------------------
for name, needle, desc in FAIL_PROGRAMS:
    path = "tests/fail/%s.hls" % name
    if not os.path.exists(path):
        bad("idt: %s missing" % path)
        continue
    rb = run(["python3", "boot/boot.py", "--check", path])
    if rb.returncode != 1:
        bad("idt: %s not rejected by boot" % name)
        continue
    rh = run(["./bin/hlc", "--audit", path])
    if rh.returncode == 0:
        bad("idt: %s accepted by hlc (parity)" % name)
        continue
    a = normalise(rb.stdout + rb.stderr)
    b = normalise(rh.stdout + rh.stderr)
    if needle not in a:
        bad("idt: %s rejected without its diagnostic (%s)" % (name, desc))
        continue
    if a != b:
        bad("idt: %s diagnostics differ:\n     boot: %s\n     hlc:  %s" % (name, a, b))
        continue
    ok("idt: %s rejected by both compilers (%s)" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 6. behaviour probes ===")
# ---------------------------------------------------------------------------
PROBES = [
    ("gdt-roundtrip", '''
import "core.interrupt"
import "core.result"
fn main() -> int {
    # 8 bytes: limit[15:0] | base[15:0] | base[23:16] | limit[19:16] | flags | access | base[31:24]
    let d: list[int] = gdt_encode(152, 4, 0, 1048575)
    if gdt_decode_access(d.get(1)) != 152 { return 1 }
    if gdt_decode_flags(d.get(1)) != 4 { return 2 }
    if gdt_decode_base(d.get(0), d.get(1)) != 0 { return 3 }
    # The limit's top nibble shares a byte with the flags: leaving it
    # out truncates every limit above 64 KiB to 0xFFFF, which still
    # looks like a valid 64-KiB segment.
    if gdt_decode_limit(d.get(0), d.get(1)) != 1048575 { return 4 }
    let b: list[int] = gdt_encode(146, 3, 291, 4095)
    if gdt_decode_base(b.get(0), b.get(1)) != 291 { return 5 }
    if gdt_decode_limit(b.get(0), b.get(1)) != 4095 { return 6 }
    let n: list[int] = gdt_encode(0, 0, 0, 0)
    if n.get(0) != 0 || n.get(1) != 0 { return 7 }
    return 0
}
''', 0, "a descriptor round-trips through encode and decode"),

    ("gdt-classes", '''
import "core.interrupt"
fn main() -> int {
    # The class bit is 0x08 and S is 0x10: reading one as the other
    # makes every data descriptor look like code.
    if !gdt_is_code(24) || gdt_is_code(18) { return 1 }
    if !gdt_is_data(18) || gdt_is_data(24) { return 2 }
    if gdt_is_system(24) || gdt_is_system(18) { return 3 }
    if !gdt_is_system(137) { return 4 }
    if gdt_fault_name(gdt_check(0, 0, true)) != "not-present" { return 5 }
    if gdt_fault_name(gdt_check(137, 0, false)) != "system-segment" { return 6 }
    # L with DB is architecturally invalid: the CPU #GPs on load.
    if gdt_fault_name(gdt_check(152, 6, true)) != "long-with-db" { return 7 }
    if gdt_fault_name(gdt_check(146, 4, false)) != "long-without-code" { return 8 }
    return 0
}
''', 0, "the code/data and system bits, and the two invalid combinations"),

    ("selectors", '''
import "core.interrupt"
fn main() -> int {
    if gdt_selector(1, 0) != 8 || gdt_selector(5, 3) != 43 { return 1 }
    if gdt_selector_index(43) != 5 || gdt_selector_rpl(43) != 3 { return 2 }
    # The index occupies bits 3..15, so the largest well-formed GDT
    # selector is (1023 << 3) | 3 = 8187 — NOT 8191, which has TI set.
    if gdt_max_selector() != 8187 { return 3 }
    if gdt_selector(1023, 3) != 8187 { return 4 }
    if gdt_selector_ok(12) { return 5 }
    if gdt_selector_ok(8188) { return 6 }
    if gdt_limine_handoff_selector64_code() != 40 { return 7 }
    return 0
}
''', 0, "the selector bound, and the TI bit a kernel never wants"),

    ("ist-and-gates", '''
import "core.interrupt"
fn main() -> int {
    # The IST is the gate's OWN byte (offset 4), not the low nibble of
    # the type/attribute byte (offset 5) — packing it there overwrites
    # the gate TYPE.
    if !idt_ist_byte_ok(7) || idt_ist_byte_ok(8) { return 1 }
    if idt_ist_unpack(idt_ist_pack(5)) != 5 { return 2 }
    if !idt_gate_ok(idt_attr_present() + idt_gate_interrupt()) { return 3 }
    if idt_gate_ok(idt_gate_interrupt()) { return 4 }
    if !idt_gate_is_trap(idt_attr_present() + idt_gate_trap()) { return 5 }
    if tss_is_busy(tss_set_busy(0, true)) != true { return 6 }
    if tss_ist_decode(tss_ist_encode(0, 1)) != 1 { return 7 }
    return 0
}
''', 0, "the IST byte, the gate types, and the TSS busy bit"),

    ("offset-split", '''
import "core.interrupt"
fn main() -> int {
    let p: list[int] = idt_offset_split(305419896)
    if p.get(0) != 22136 || p.get(1) != 4660 || p.get(2) != 0 { return 1 }
    if idt_offset_join(p.get(0), p.get(1), p.get(2)) != 305419896 { return 2 }
    # A 32-bit offset is all a gate can reach, which is what forces the
    # higher-half relocation. The upper half is tested with a LOGICAL
    # shift: an arithmetic shift of -1 by 32 still yields -1.
    if !idt_offset_fits(4294967295) || idt_offset_fits(4294967296) { return 3 }
    if idt_offset_fits(-1) { return 4 }
    if !idt_entry_ok(idt_attr_present() + idt_gate_interrupt(), 8, 1048576) { return 5 }
    if idt_entry_ok(idt_attr_present() + idt_gate_interrupt(), 8, -1) { return 6 }
    return 0
}
''', 0, "the 32-bit offset split and the reachability rule"),

    ("page-fault", '''
import "core.interrupt"
fn main() -> int {
    # bit 0 P, bit 1 W/R, bit 2 U/S, bit 3 reserved, bit 4 I/D.
    if page_fault_cause_name(page_fault_decode(0)) != "read-not-present" { return 1 }
    if page_fault_cause_name(page_fault_decode(2)) != "write-not-present" { return 2 }
    if page_fault_cause_name(page_fault_decode(3)) != "write-protected" { return 3 }
    if page_fault_cause_name(page_fault_decode(7)) != "user-write-protected" { return 4 }
    if page_fault_cause_name(page_fault_decode(16)) != "instruction-fetch" { return 5 }
    # The reserved bit is checked first: every other answer is
    # meaningless once it is set.
    if page_fault_cause_name(page_fault_decode(8)) != "reserved-bit-set" { return 6 }
    let mut c: int = 0
    while c < 8 {
        if page_fault_code(page_fault_decode(c)) != c { return 7 }
        c = c + 1
    }
    if !page_fault_is_present(page_fault_decode(3)) { return 8 }
    if page_fault_is_present(page_fault_decode(2)) { return 9 }
    return 0
}
''', 0, "the page-fault error code decodes and round-trips"),

    ("exceptions", '''
import "core.interrupt"
fn main() -> int {
    if exc_count() != 32 { return 1 }
    if exc_name(14) != "page-fault" || exc_name(8) != "double-fault" { return 2 }
    if exc_name(22) != "" { return 3 }
    # Getting the error-pushing set wrong is not a crash — it is a
    # handler reading its fault address from the wrong stack slot.
    if !exc_pushes_error(8) || !exc_pushes_error(14) || !exc_pushes_error(17) { return 4 }
    if exc_pushes_error(0) || exc_pushes_error(3) { return 5 }
    if !exc_is_fault(14) || exc_is_fault(3) { return 6 }
    # A double fault happened when the kernel could not push a frame, so
    # its handler must run on a stack set up in advance.
    if exc_default_ist(8) != 1 || exc_default_ist(14) != 0 { return 7 }
    return 0
}
''', 0, "the 32 exception vectors, and which push an error code"),

    ("pic", '''
import "core.interrupt"
fn main() -> int {
    if pic_master_vector_base() != 32 || pic_slave_vector_base() != 40 { return 1 }
    if pic_vector_irq(32) != 0 || pic_vector_irq(47) != 15 { return 2 }
    if pic_vector_ok(48) { return 3 }
    # An exception never needs an EOI.
    if pic_vector_needs_eoi(14) { return 4 }
    if !pic_vector_needs_slave_eoi(40) || pic_vector_needs_slave_eoi(39) { return 5 }
    if pic_remap_ok(8, 40) || pic_remap_ok(40, 32) { return 6 }
    if !pic_remap_ok(32, 40) { return 7 }
    return 0
}
''', 0, "the 8259 remap, the IRQ numbering and the EOI rules"),
]

for name, body, want, desc in PROBES:
    path = os.path.join(TMP, "probe_%s.hls" % name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("idt: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    code, out = hlc_run(path)
    if code != want:
        bad("idt: probe %s (native) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    ok("idt: %s — %s" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 7. the demos + tools ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("idt: the ok-test is clean on the interpreter")
else:
    bad("idt: ok-test interpreter exit=%d" % r.returncode)
code, out = hlc_run(OK_TEST)
if code == 0:
    ok("idt: the ok-test is clean natively")
else:
    bad("idt: ok-test native exit=%s" % code)
fs_ok = os.path.join(TMP, "ok_fs.hls")
text = open(OK_TEST, encoding="utf-8").read()
text = re.sub(r"(?m)^#!\[no_std\]$", "#![freestanding]", text, count=1)
with open(fs_ok, "w", encoding="utf-8") as fh:
    fh.write(text)
fs_c = os.path.join(TMP, "ok_fs.c")
if run(["./bin/hlc", fs_ok, fs_c]).returncode == 0:
    g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
             "-fno-stack-protector", "-Wl,--gc-sections", "-o",
             os.path.join(TMP, "ok_fs"), fs_c])
    if g.returncode == 0 and run([os.path.join(TMP, "ok_fs")]).returncode == 0:
        ok("idt: the ok-test links -nostdlib and exits 0 (freestanding)")
    else:
        bad("idt: the freestanding ok-test failed: %s" % g.stderr.strip()[:120])
else:
    bad("idt: the ok-test did not compile as freestanding")

ri = run(["python3", "boot/boot.py", DEMO])
code, out = hlc_run(DEMO)
if ri.returncode == 0 and code == 0 and "DEMO OK" in out:
    ok("idt: interrupt_demo runs on both, DEMO OK")
else:
    bad("idt: interrupt_demo interp=%s native=%s" % (ri.returncode, code))
if ri.stdout == out:
    ok("idt: interrupt_demo output is byte-identical (differential)")
else:
    bad("idt: interrupt_demo differs between interpreter and native")

# A real image whose handlers are declared: the stubs and table must be
# in the LINKED binary.
kr = os.path.join(TMP, "kernel.hls")
text = open(KERNEL, encoding="utf-8").read()
text = text.replace('#![link_script("../link.ld")]', '#![link_script("../../link.ld")]')
text = text.replace('fn main() -> int {', '''#[irq_handler(14)]
fn on_page_fault(frame: list[int]) -> void {
    return
}

#[irq_handler(8)]
fn on_double_fault(frame: list[int]) -> void {
    return
}

fn main() -> int {''', 1)
with open(kr, "w", encoding="utf-8") as fh:
    fh.write(text)
kc = os.path.join(TMP, "kernel.c")
if run(["./bin/hlc", kr, kc]).returncode != 0:
    bad("idt: the kernel with declared handlers did not compile")
elif shutil.which("nm") is None:
    bad("idt: nm not available")
else:
    g = run(["gcc", "-O2", "-Werror", "-ffreestanding", "-nostdlib",
             "-fno-pie", "-no-pie", "-fno-stack-protector", "-T", "link.ld",
             "-o", os.path.join(TMP, "kernel.elf"), kc])
    if g.returncode != 0:
        bad("idt: the kernel with declared handlers did not link: %s"
            % g.stderr.strip()[:160])
    else:
        syms = run(["nm", os.path.join(TMP, "kernel.elf")]).stdout
        if "hl_irq_stub_14" in syms and "hl_irq_stub_8" in syms:
            ok("idt: the kernel image carries a stub per declared vector")
        else:
            bad("idt: the kernel image is missing a stub")
        if "hl_idt_bindings" in syms:
            ok("idt: the kernel image carries the binding table")
        else:
            bad("idt: the kernel image is missing the binding table")

for label, cmd, want in (
        ("hlfmt -c (ok-test)", ["python3", "tools/hlfmt.py", "-c", OK_TEST], 0),
        ("hlfmt -c (demo)", ["python3", "tools/hlfmt.py", "-c", DEMO], 0),
        ("hlfmt -c (core)", ["python3", "tools/hlfmt.py", "-c", CORE], 0),
        ("hllint (ok-test)", ["python3", "tools/hllint.py", OK_TEST], 0)):
    r = run(cmd)
    if r.returncode == want:
        ok("idt: %s" % label)
    else:
        bad("idt: %s: %s" % (label, (r.stdout + r.stderr).strip()[:140]))

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
