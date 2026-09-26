#!/usr/bin/env python3
"""Stage 88 acceptance gate — core.port: x86 I/O ports.

Run with `make port-acceptance` (or `python3 tests/port_acceptance.py`).

Seven sections:

  1. the carrier rule         — a sub-64 register is accepted for an
                                  `int` operand when the template's
                                  instruction is that narrow, the two
                                  widths one port instruction needs
                                  (8-bit data, 16-bit port) each get
                                  their own carrier, and a 64-bit
                                  instruction on a narrow register is
                                  still rejected
  2. core/port.hls             — the enum / surface, the
                                  `core.*`-only import, no effects
  3. the PATTERN               — the demo's six port instructions
                                  compile `-Werror` and LINK, and the
                                  disassembly really is
                                  `out %al,(%dx)` / `in (%dx),%eax`
  4. the demo                  — it runs, because `main` touches no
                                  port: a port access is privileged
                                  and an image run by a host faults on
                                  the first one
  5. the model                 — the port map, the widths each
                                  architectural port actually uses,
                                  the PIC/PIT/PCI/ACPI tables, on all
                                  three execution paths
  6. behaviour probes          — the width rules, the 16-bit port
                                  bound, the EOI order, the PIT
                                  divisor wrap, the PCI field widths
  7. the tools                 — hlfmt and hllint
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
TMP = tempfile.mkdtemp(prefix="_gate_port_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage88_port.hls"
DEMO = "examples/port_demo.hls"
CORE = "core/port.hls"

OUT8_HLS = ('fn out8(p: int, v: int) -> void {\n'
            '    asm!("outb {0}, {1}", in("a") v, in("dx") p)\n'
            '    return\n}\n\nfn main() -> int { return 0 }\n')
IN8_HLS = ('fn in8(p: int) -> int {\n'
           '    let mut v: int = 0\n'
           '    asm!("inb {1}, {0}", out("al") v, in("dx") p)\n'
           '    return v\n}\n\nfn main() -> int { return 0 }\n')
WIDE_HLS = ('fn rd(a: int) -> int {\n'
            '    let mut v: int = 0\n'
            '    asm!("movq ({1}), {0}", out("al") v, in("cx") a)\n'
            '    return v\n}\n\nfn main() -> int { return 0 }\n')

CORE_FNS = [
    "port_number_bits", "port_max", "port_is_legal", "port_has_immediate",
    "port_imm_ok", "port_width_name", "port_data_bits", "port_width_suffix",
    "port_width_mask", "port_read", "port_write", "pic_cmd_port",
    "pic_data_port", "pic_slave_cmd_port", "pic_slave_data_port", "pic_icw1",
    "pic_icw4_8086", "pic_ocw2_eoi", "pic_ocw3_isr_read",
    "pic_eoi_order_is_slave_first", "pit_channel0_port", "pit_command_port",
    "pit_freq", "pit_divisor", "pit_reload_low", "pit_reload_high",
    "speaker_gate_port", "debug_port", "debug_write_byte",
    "pci_config_address_port", "pci_config_data_port", "pci_bus_bits",
    "pci_device_bits", "pci_function_bits", "pci_register_bits",
    "pci_config_address", "pci_field_fits", "pm1_status_port",
    "pm1_status_write1_to_clear", "port_delay_cycles",
    "port_access_cost_cycles",
]

PROBES = [
    ("widths", """import "core.port"
fn main() -> int {
    if port_number_bits() != 16 { return 1 }
    if port_max() != 65535 { return 2 }
    if port_width_mask(PortWidth.Byte) != 255 { return 3 }
    if port_width_suffix(PortWidth.Dword) != "l" { return 4 }
    # An 8-bit in puts the byte in AL and LEAVES the rest alone.
    if port_read(1311768467463791224, PortWidth.Dword) != 2596070008 { return 5 }
    if port_write(300, PortWidth.Byte) != 44 { return 6 }
    if !port_is_legal(0) || port_is_legal(65536) { return 7 }
    # The IMMEDIATE form is a different encoding and exists only below
    # 256.
    if port_has_immediate(256) || !port_imm_ok(248) { return 8 }
    return 0
}
""", 0, "the width masks, the 16-bit port bound, and the imm8 form"),
    ("pic", """import "core.port"
fn main() -> int {
    if pic_cmd_port() != 32 || pic_data_port() != 33 { return 1 }
    if pic_slave_data_port() != 161 { return 2 }
    # ICW4's 8086 bit: a PIC left in 8085 mode does not deliver an
    # interrupt to a protected-mode kernel.
    if pic_icw1() != 17 || pic_icw4_8086() != 1 { return 3 }
    if pic_ocw2_eoi() != 32 { return 4 }
    # The SLAVE's EOI goes FIRST for IRQ 8..15.
    if !pic_eoi_order_is_slave_first(9) { return 5 }
    if pic_eoi_order_is_slave_first(7) { return 6 }
    return 0
}
""", 0, "the PIC ports, the 8086 bit, and the EOI order"),
    ("pit", """import "core.port"
fn main() -> int {
    if pit_freq() != 1193182 { return 1 }
    if pit_divisor(100) != 11931 { return 2 }
    # A divisor of 0 means 65536: the counter is 16-bit and WRAPS.
    if pit_divisor(1) != 65536 { return 3 }
    if pit_divisor(1193182) != 1 { return 4 }
    # The two bytes go LOW FIRST. That is not a convention.
    let d: int = pit_divisor(100)
    if pit_reload_low(d) != 155 { return 5 }
    if pit_reload_high(d) != 46 { return 6 }
    if speaker_gate_port() != 97 { return 7 }
    return 0
}
""", 0, "the PIT divisor, its 65536 wrap, and the low-byte-first order"),
    ("pci", """import "core.port"
fn main() -> int {
    if pci_config_address_port() != 248 || pci_config_data_port() != 252 { return 1 }
    # Both ports are 32-bit, but the bus is 8 bits, the device 5, the
    # function 3 and the register 4.
    if pci_bus_bits() != 8 || pci_register_bits() != 4 { return 2 }
    if pci_device_bits() != 5 || pci_function_bits() != 3 { return 3 }
    let a: int = pci_config_address(0, 3, 0, 4)
    if a < 2147483648 { return 4 }
    # Each field has to be MASKED: a bare shift reads the enable bit.
    if int_and(int_shr(a, 11), 31) != 3 { return 5 }
    if int_and(int_shr(a, 2), 15) != 4 { return 6 }
    if pci_vendor_id_reg() != 0 || pci_bar0_reg() != 16 { return 7 }
    return 0
}
""", 0, "the PCI field widths, and why each field must be masked"),
    ("acpi-delay", """import "core.port"
fn main() -> int {
    if pm1_control_port() != 0 || pm1_status_port() != 4 { return 1 }
    # PM1 status is write-1-to-clear: writing ZERO clears nothing, which
    # is a hang waiting for a bit that has been acknowledged.
    if pm1_status_write1_to_clear() != true { return 2 }
    # The serialisation delay is a MODEL, so a test asserts the
    # arithmetic rather than a timing.
    if port_delay_cycles() != 400 { return 3 }
    if port_access_cost_cycles(PortWidth.Byte) != 408 { return 4 }
    if port_access_cost_cycles(PortWidth.Dword) != 432 { return 5 }
    return 0
}
""", 0, "the ACPI PM1 status rule and the I/O serialisation model"),
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


def write(name, text):
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def compile_to(src, c):
    return run(["./bin/hlc", src, c])


# ---------------------------------------------------------------------------
print("=== 1. the carrier rule ===")
# ---------------------------------------------------------------------------
c = os.path.join(TMP, "out8.c")
r = compile_to(write("out8.hls", OUT8_HLS), c)
if r.returncode == 0:
    ok("port: an 8-bit letter constraint with a 16-bit dx port is accepted")
else:
    bad("port: the outb form rejected: %s" % (r.stdout + r.stderr)[:140])
text = open(c, encoding="utf-8", errors="replace").read() if os.path.exists(c) else ""
if '(int8_t)' in text and '(int16_t)' in text:
    ok("port: the two widths of one instruction get two carriers")
else:
    bad("port: the carriers are not per-operand widths")

c = os.path.join(TMP, "in8.c")
r = compile_to(write("in8.hls", IN8_HLS), c)
if r.returncode == 0:
    ok("port: an 8-bit output binds a sub-register through a register variable")
else:
    bad("port: the inb form rejected: %s" % (r.stdout + r.stderr)[:140])
text_in = open(c, encoding="utf-8", errors="replace").read() if os.path.exists(c) else ""
if re.search(r'register int8_t \w+ __asm__\("al"\)', text_in):
    ok("port: the 8-bit output is an int8_t register variable named al")
else:
    bad("port: no int8_t register variable for al")

r = compile_to(write("wide.hls", WIDE_HLS), os.path.join(TMP, "wide.c"))
if r.returncode != 0 and "64-bit register" in (r.stdout + r.stderr):
    ok("port: a 64-bit template on a narrow register is still rejected")
else:
    bad("port: a narrow register on movq was accepted")

# ---------------------------------------------------------------------------
print("=== 2. core/port.hls ===")
# ---------------------------------------------------------------------------
if not os.path.exists(CORE):
    bad("port: core/port.hls missing")
else:
    src = open(CORE, encoding="utf-8").read()
    if re.search(r"enum\s+PortWidth\b", src):
        ok("port: core.port declares enum PortWidth")
    else:
        bad("port: core.port missing enum PortWidth")
    imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
    if imports == ["core.result"]:
        ok("port: core.port imports only core.result (freestanding-safe)")
    else:
        bad("port: core.port imports %r" % imports)
    decls = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    if not re.search(r"^fn[^\n]*\buses\b", decls, re.M) and not re.search(
            r"^\s*extern\b", decls, re.M):
        ok("port: core.port declares no effects and no externs")
    else:
        bad("port: core.port declares effects or externs")
    for fn in CORE_FNS:
        if re.search(r"fn\s+%s\b" % fn, src):
            ok("port: core.port exposes %s" % fn)
        else:
            bad("port: core.port missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 3. the PATTERN ===")
# ---------------------------------------------------------------------------
demo_c = os.path.join(TMP, "demo.c")
r = compile_to(DEMO, demo_c)
if r.returncode != 0:
    bad("port: the demo did not compile: %s" % (r.stdout + r.stderr)[:160])
else:
    ok("port: the demo compiles")
    demo_elf = os.path.join(TMP, "demo.elf")
    g = run(["gcc", "-O2", "-Werror", "-ffreestanding", "-nostdlib", "-fno-pie",
             "-no-pie", "-ffunction-sections", "-fno-stack-protector",
             "-T", "link.ld", "-o", demo_elf, demo_c])
    if g.returncode != 0:
        bad("port: the demo did not link -Werror: %s" % g.stderr.strip()[:160])
    elif shutil.which("objdump") is None:
        bad("port: objdump not available")
    else:
        ok("port: the demo links -Werror as a freestanding image")
        dis = run(["objdump", "-d", demo_elf]).stdout
        # The six real instructions, as the hardware spells them.
        for want, why in (("out    %al,(%dx)", "outb"),
                          ("in     (%dx),%al", "inb"),
                          ("out    %ax,(%dx)", "outw"),
                          ("in     (%dx),%ax", "inw"),
                          ("out    %eax,(%dx)", "outl"),
                          ("in     (%dx),%eax", "inl")):
            if want in dis:
                ok("port: the image really contains %s" % why)
            else:
                bad("port: %s is missing from the disassembly" % why)
        # `main` must touch NO port: a port access is privileged and the
        # demo has to be runnable by the gate.
        main_dis = ""
        started = False
        for l in dis.split("\n"):
            if "<usf_main>:" in l:
                started = True
                continue
            if started and re.match(r"^[0-9a-f]+ <", l):
                break
            if started:
                main_dis += l + "\n"
        if re.search(r"\b(out|in)\s", main_dis):
            bad("port: main touches a port, so the demo cannot run")
        else:
            ok("port: main issues no port access (the demo is runnable)")

# ---------------------------------------------------------------------------
print("=== 4. the demo ===")
# ---------------------------------------------------------------------------
p = run([os.path.join(TMP, "demo.elf")]) if os.path.exists(os.path.join(TMP, "demo.elf")) else None
if p is None:
    bad("port: the demo image was not built")
elif p.returncode == 0:
    ok("port: the demo image runs, exit 0")
else:
    bad("port: the demo image exit=%d" % p.returncode)

# ---------------------------------------------------------------------------
print("=== 5. the model ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("port: the ok-test is clean on the interpreter")
else:
    bad("port: ok-test interpreter exit=%d" % r.returncode)
c = os.path.join(TMP, "ok.c")
if compile_to(OK_TEST, c).returncode == 0:
    exe = os.path.join(TMP, "ok")
    g = run(["gcc", "-O2", "-Werror", "-o", exe, c, "-lm", "-pthread"])
    if g.returncode == 0 and run([exe]).returncode == 0:
        ok("port: the ok-test is clean natively")
    else:
        bad("port: the ok-test failed natively: %s" % g.stderr.strip()[:140])
else:
    bad("port: the ok-test did not compile")
text = open(OK_TEST, encoding="utf-8").read()
text = re.sub(r"(?m)^#!\[no_std\]$", "", text, count=1)
fs = os.path.join(TMP, "ok_fs.hls")
with open(fs, "w", encoding="utf-8") as fh:
    fh.write("#![freestanding]\n" + text)
c = os.path.join(TMP, "ok_fs.c")
if compile_to(fs, c).returncode == 0:
    exe = os.path.join(TMP, "ok_fs")
    g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
             "-fno-stack-protector", "-Wl,--gc-sections", "-o", exe, c])
    if g.returncode == 0 and run([exe]).returncode == 0:
        ok("port: the ok-test links -nostdlib and exits 0")
    else:
        bad("port: the freestanding ok-test failed: %s" % g.stderr.strip()[:140])
else:
    bad("port: the ok-test did not compile as freestanding")

# ---------------------------------------------------------------------------
print("=== 6. behaviour probes ===")
# ---------------------------------------------------------------------------
for name, body, want, desc in PROBES:
    path = write("probe_%s.hls" % name, body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("port: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    c = os.path.join(TMP, "probe_%s.c" % name)
    if compile_to(path, c).returncode != 0:
        bad("port: probe %s did not compile" % name)
        continue
    exe = os.path.join(TMP, "probe_%s" % name)
    g = run(["gcc", "-O2", "-Werror", "-o", exe, c, "-lm", "-pthread"])
    if g.returncode != 0:
        bad("port: probe %s did not link" % name)
        continue
    p = run([exe])
    if p.returncode != want:
        bad("port: probe %s (native) exit=%d want=%d — %s"
            % (name, p.returncode, want, p.stdout.strip()[:100]))
        continue
    fs = os.path.join(TMP, "probe_%s_fs.hls" % name)
    with open(fs, "w", encoding="utf-8") as fh:
        fh.write("#![freestanding]\n" + body)
    c = os.path.join(TMP, "probe_%s_fs.c" % name)
    if compile_to(fs, c).returncode == 0:
        exe = os.path.join(TMP, "probe_%s_fs" % name)
        g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
                 "-fno-stack-protector", "-Wl,--gc-sections", "-o", exe, c])
        if g.returncode != 0 or run([exe]).returncode != want:
            bad("port: probe %s (-nostdlib) failed" % name)
            continue
    else:
        bad("port: probe %s did not compile freestanding" % name)
        continue
    ok("port: %s — %s" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 7. the tools ===")
# ---------------------------------------------------------------------------
for label, cmd in (
        ("hlfmt -c (ok-test)", ["python3", "tools/hlfmt.py", "-c", OK_TEST]),
        ("hlfmt -c (demo)", ["python3", "tools/hlfmt.py", "-c", DEMO]),
        ("hlfmt -c (core)", ["python3", "tools/hlfmt.py", "-c", CORE]),
        ("hllint (ok-test)", ["python3", "tools/hllint.py", OK_TEST])):
    r = run(cmd)
    if r.returncode == 0:
        ok("port: %s" % label)
    else:
        bad("port: %s: %s" % (label, (r.stdout + r.stderr).strip()[:140]))

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
