#!/usr/bin/env python3
"""Stage 87 acceptance gate — core.mmio: volatile device access.

Run with `make mmio-acceptance` (or `python3 tests/mmio_acceptance.py`).

Seven sections:

  1. the register rule         — a 32-bit register is accepted for an
                                  `int` operand exactly when the
                                  template's instruction is 32-bit, and
                                  still rejected otherwise
  2. core/mmio.hls              — the enums / structs / surface, the
                                  `core.*`-only import, no effects
  3. the PATTERN                — the demo's `asm!` accesses compile
                                  `-Werror`, and the emitted C really
                                  is a volatile access with the memory
                                  clobber
  4. volatility                — a volatile DEVICE read is not folded:
                                  two `rdtsc` reads with a million
                                  iterations between them must differ,
                                  hosted AND -nostdlib
  5. the checked constructors  — four run-time panics, accepted by the
                                  checker
  6. behaviour probes          — widths, masks, sign extension,
                                  alignment, windows, bit fields,
                                  barriers, domains, the LAPIC map
  7. the demo + tools          — mmio_demo runs natively, the ok-test
                                  is clean on all three paths, hlfmt
                                  and hllint are clean
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
TMP = tempfile.mkdtemp(prefix="_gate_mmio_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage87_mmio.hls"
DEMO = "examples/mmio_demo.hls"
CORE = "core/mmio.hls"

PANIC_PROGRAMS = [
    ("panic_stage87_window_base", "a device window must be page-aligned",
     "a non-page-aligned MMIO base"),
    ("panic_stage87_field_mask", "width must be 1..64",
     "a bit field wider than the register"),
    ("panic_stage87_read_field", "does not fit in a 64-bit register",
     "a field ending past bit 63"),
    ("panic_stage87_lapic", "register id must be >= 0",
     "a negative LAPIC register id"),
]

# The register-rule programs, written out rather than generated so the
# exact spelling the compiler sees is visible in the gate.
NARROW_HLS = (
    'fn rd(a: int) -> int {\n'
    '    let mut v: int = 0\n'
    '    asm!("movl ({1}), {0}", out("eax") v, in("ecx") a)\n'
    '    return v\n'
    '}\n\nfn main() -> int { return 0 }\n')
WIDE_HLS = (
    'fn rd(a: int) -> int {\n'
    '    let mut v: int = 0\n'
    '    asm!("movq ({1}), {0}", out("eax") v, in("ecx") a)\n'
    '    return v\n'
    '}\n\nfn main() -> int { return 0 }\n')
TSC_HLS = (
    'fn rd() -> int {\n'
    '    let mut lo: int = 0\n'
    '    let mut hi: int = 0\n'
    '    asm!("rdtsc", out("rax") lo, out("rdx") hi)\n'
    '    return hi\n'
    '}\n\nfn main() -> int { return 0 }\n')
VOL_HLS = (
    'fn rd() -> int {\n'
    '    let mut lo: int = 0\n'
    '    let mut hi: int = 0\n'
    '    asm!("rdtsc", out("rax") lo, out("rdx") hi)\n'
    '    return int_or(int_shl(int_and(hi, 4294967295), 32), int_and(lo, 4294967295))\n'
    '}\n\n'
    'fn main() -> int {\n'
    '    let a: int = rd()\n'
    '    let mut i: int = 0\n'
    '    while i < 4000000 {\n'
    '        i = i + 1\n'
    '    }\n'
    '    let b: int = rd()\n'
    '    if b < a {\n'
    '        return 1\n'
    '    }\n'
    '    if b == a {\n'
    '        return 2\n'
    '    }\n'
    '    return 0\n'
    '}\n')
ALIGNED_HLS = (
    'import "core.result"\n\n'
    'fn main() -> int {\n'
    '    let xs: list[int] = [1, 2, 3, 4, 5, 6, 7, 8]\n'
    '    let mut i: int = 0\n'
    '    let mut sum: int = 0\n'
    '    while i < xs.len() {\n'
    '        sum = sum + xs.get(i)\n'
    '        i = i + 1\n'
    '    }\n'
    '    if sum != 36 {\n'
    '        return 1\n'
    '    }\n'
    '    return 0\n'
    '}\n')

PROBES = [
    ("widths", """import "core.mmio"
fn main() -> int {
    if mmio_width_mask(MmioWidth.Dword) != 4294967295 { return 1 }
    if mmio_width_mask(MmioWidth.Qword) != -1 { return 2 }
    let poisoned: int = 1311768467463791224
    if mmio_read(poisoned, MmioWidth.Dword) != 2596070008 { return 3 }
    if mmio_read(poisoned, MmioWidth.Byte) != 120 { return 4 }
    if mmio_read(poisoned, MmioWidth.Qword) != poisoned { return 5 }
    if mmio_width_suffix(MmioWidth.Dword) != "l" { return 6 }
    return 0
}
""", 0, "the width masks, and the upper half a 32-bit read leaves behind"),
    ("sign-extend", """import "core.mmio"
fn main() -> int {
    if mmio_read_signed(251, MmioWidth.Byte) != -5 { return 1 }
    if mmio_read_signed(5, MmioWidth.Byte) != 5 { return 2 }
    if mmio_read_signed(65534, MmioWidth.Word) != -2 { return 3 }
    return 0
}
""", 0, "sign extension fills bits n..63, not just the sign bit"),
    ("windows", """import "core.mmio"
fn main() -> int {
    let w: MmioRegion = mmio_region_new(67108864, 8192, false)
    if !mmio_access_ok(w, 67108864 + 8, MmioWidth.Dword) { return 1 }
    if mmio_access_ok(w, 67108864 + 8190, MmioWidth.Dword) { return 2 }
    if mmio_access_ok(w, 67108864 + 1, MmioWidth.Dword) { return 3 }
    if !mmio_region_contains(w, 67117055) { return 4 }
    if mmio_region_contains(w, 67117056) { return 5 }
    let odd: MmioRegion = mmio_region_new(67125248, 6, false)
    if mmio_access_ok(odd, 67125248 + 4, MmioWidth.Dword) { return 6 }
    if !mmio_access_ok(odd, 67125248, MmioWidth.Dword) { return 7 }
    if mmio_region_find([w, odd], 0) != -1 { return 8 }
    return 0
}
""", 0, "a window answers whether an access is legal, in one predicate"),
    ("fields", """import "core.mmio"
fn main() -> int {
    let f: MmioField = mmio_field_new(4, 3)
    let wide: MmioField = mmio_field_new(4, 12)
    let split: MmioField = mmio_field_new(30, 4)
    let whole: MmioField = mmio_field_new(0, 64)
    if mmio_field_mask(f) != 112 { return 1 }
    if mmio_read_field(240, f) != 7 { return 2 }
    if mmio_write_field(0, f, 255) != 112 { return 3 }
    if mmio_set_field(0, f) != 112 { return 4 }
    if mmio_clear_field(127, f) != 15 { return 5 }
    if !mmio_field_straddles32(split) { return 6 }
    if mmio_field_write_ok(split, MmioWidth.Dword) { return 7 }
    if !mmio_field_write_ok(split, MmioWidth.Qword) { return 8 }
    if mmio_field_write_ok(wide, MmioWidth.Byte) { return 9 }
    if mmio_field_mask(whole) != -1 { return 10 }
    return 0
}
""", 0, "the read-modify-write arithmetic, and the two hazard cases"),
    ("domains", """import "core.mmio"
fn main() -> int {
    if mmio_barrier_name(mmio_default_barrier()) != "cpu" { return 1 }
    if !mmio_is_fence(mmio_default_barrier()) { return 2 }
    if mmio_is_fence(MmioBarrier.Compiler) { return 3 }
    if mmio_domain_needs_cache_maintenance(MmioDomain.Uncacheable) { return 4 }
    if !mmio_domain_needs_barrier(MmioDomain.DeviceNgnRnE) { return 5 }
    if mmio_domain_needs_barrier(MmioDomain.Normal) { return 6 }
    return 0
}
""", 0, "the barrier levels, and the x86/aarch64 difference"),
    ("lapic", """import "core.mmio"
fn main() -> int {
    if !lapic_map_valid() { return 1 }
    if lapic_reg_addr(lapic_reg_id()) != 8 { return 2 }
    if lapic_reg_addr(lapic_reg_version()) != 12 { return 3 }
    if lapic_reg_addr(lapic_reg_svr()) != 60 { return 4 }
    if lapic_decode_delivery(lapic_encode_delivery(0, lapic_delivery_nmi())) != 2 { return 5 }
    if lapic_svr_vector(lapic_svr_encode(true, 255)) != 255 { return 6 }
    if !lapic_lvt_is_enabled(lapic_lvt_masked()) { return 7 }
    return 0
}
""", 0, "the LAPIC map, the delivery mode, and the SVR's two fields"),
]

CORE_FNS = [
    "mmio_width_mask", "mmio_width_bits", "mmio_width_suffix", "mmio_read",
    "mmio_read_signed", "mmio_write", "mmio_sign_extend", "mmio_is_aligned",
    "mmio_is_page_aligned", "mmio_region_new", "mmio_region_contains",
    "mmio_region_end", "mmio_access_fits", "mmio_access_ok",
    "mmio_region_find", "mmio_overlaps", "mmio_regions_disjoint",
    "mmio_regions_total", "mmio_field_new", "mmio_field_mask", "mmio_field_ok",
    "mmio_field_straddles32", "mmio_read_field", "mmio_read_field_signed",
    "mmio_write_field", "mmio_set_field", "mmio_clear_field",
    "mmio_toggle_field", "mmio_field_write_ok", "mmio_barrier_name",
    "mmio_default_barrier", "mmio_is_fence", "mmio_domain_needs_barrier",
    "mmio_domain_needs_cache_maintenance", "lapic_map_valid", "lapic_reg_addr",
    "lapic_reg_aligned", "lapic_reg_fits", "lapic_encode_delivery",
    "lapic_decode_delivery", "lapic_svr_encode", "lapic_svr_vector",
    "lapic_lvt_masked",
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


def write(name, text, freestanding=False):
    path = os.path.join(TMP, name)
    body = ("#![freestanding]\n" if freestanding else "") + text
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def hlc_run(src, freestanding=False):
    c = os.path.join(TMP, os.path.basename(src) + (".fs.c" if freestanding else ".c"))
    exe = os.path.join(TMP, os.path.basename(src) + (".fs" if freestanding else ".bin"))
    r = run(["./bin/hlc", src, c])
    if r.returncode != 0:
        return None, (r.stdout + r.stderr)
    if freestanding:
        g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
                 "-fno-stack-protector", "-Wl,--gc-sections", "-o", exe, c])
    else:
        g = run(["gcc", "-O2", "-Werror", "-o", exe, c, "-lm", "-pthread"])
    if g.returncode != 0:
        return None, g.stderr
    r = run([exe])
    return r.returncode, r.stdout + r.stderr


# ---------------------------------------------------------------------------
print("=== 1. the register rule ===")
# ---------------------------------------------------------------------------
r = run(["./bin/hlc", write("narrow.hls", NARROW_HLS),
         os.path.join(TMP, "narrow.c")])
if r.returncode == 0:
    ok("mmio: a 32-bit register is accepted for a 32-bit template")
else:
    bad("mmio: 32-bit register rejected: %s" % (r.stdout + r.stderr)[:120])
text = open(os.path.join(TMP, "narrow.c"), encoding="utf-8", errors="replace").read()
if "movl" in text:
    ok("mmio: the template keeps its 32-bit mnemonic")
else:
    bad("mmio: the 32-bit mnemonic is missing")

# ...and a 64-BIT template with a 32-bit register is STILL rejected:
# that is Stage 83's truncation bug, and relaxing it was not the point.
r = run(["./bin/hlc", write("wide.hls", WIDE_HLS), os.path.join(TMP, "wide.c")])
if r.returncode != 0 and "64-bit register" in (r.stdout + r.stderr):
    ok("mmio: a 32-bit register on a 64-bit template is still rejected")
else:
    bad("mmio: a 32-bit register on movq was accepted")

r = run(["./bin/hlc", write("tsc.hls", TSC_HLS), os.path.join(TMP, "tsc.c")])
if r.returncode == 0:
    ok("mmio: rdtsc binds rax/rdx (a 64-bit instruction)")
else:
    bad("mmio: rdtsc rejected: %s" % (r.stdout + r.stderr)[:120])

# The 32-bit lowering must use an int32_t register variable, or GCC
# prints the full register name and `movl %rax` does not assemble.
if re.search(r'register int32_t \w+ __asm__\("eax"\)', text):
    ok("mmio: the 32-bit operand lowers to an int32_t register variable")
else:
    bad("mmio: no int32_t register variable for the 32-bit operand")

# ---------------------------------------------------------------------------
print("=== 2. core/mmio.hls ===")
# ---------------------------------------------------------------------------
if not os.path.exists(CORE):
    bad("mmio: core/mmio.hls missing")
else:
    src = open(CORE, encoding="utf-8").read()
    for enum in ("MmioWidth", "MmioBarrier", "MmioDomain"):
        if re.search(r"enum\s+%s\b" % enum, src):
            ok("mmio: core.mmio declares enum %s" % enum)
        else:
            bad("mmio: core.mmio missing enum %s" % enum)
    for struct in ("MmioRegion", "MmioField"):
        if re.search(r"struct\s+%s\b" % struct, src):
            ok("mmio: core.mmio declares struct %s" % struct)
        else:
            bad("mmio: core.mmio missing struct %s" % struct)
    imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
    if imports == ["core.result"]:
        ok("mmio: core.mmio imports only core.result (freestanding-safe)")
    else:
        bad("mmio: core.mmio imports %r" % imports)
    decls = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    if not re.search(r"^fn[^\n]*\buses\b", decls, re.M) and not re.search(
            r"^\s*extern\b", decls, re.M):
        ok("mmio: core.mmio declares no effects and no externs")
    else:
        bad("mmio: core.mmio declares effects or externs")
    for fn in CORE_FNS:
        if re.search(r"fn\s+%s\b" % fn, src):
            ok("mmio: core.mmio exposes %s" % fn)
        else:
            bad("mmio: core.mmio missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 3. the PATTERN ===")
# ---------------------------------------------------------------------------
r = run(["./bin/hlc", DEMO, os.path.join(TMP, "demo.c")])
if r.returncode != 0:
    bad("mmio: the demo did not compile: %s" % (r.stdout + r.stderr)[:160])
else:
    text = open(os.path.join(TMP, "demo.c"), encoding="utf-8", errors="replace").read()
    if "__asm__ __volatile__" in text:
        ok("mmio: the accesses are __volatile__")
    else:
        bad("mmio: the accesses are not volatile")
    if '"memory"' in text:
        ok("mmio: the default clobber list carries memory")
    else:
        bad("mmio: the memory clobber is missing")
    for want, why in (("movq", "the 64-bit load"),
                      ("movl", "the 32-bit load and store"),
                      ("mfence", "the fence")):
        if want in text:
            ok("mmio: the demo emits %s" % why)
        else:
            bad("mmio: %s is missing" % why)
    g = run(["gcc", "-O2", "-Werror", "-o", os.path.join(TMP, "demo"),
             os.path.join(TMP, "demo.c"), "-lm", "-pthread"])
    if g.returncode != 0:
        bad("mmio: the demo did not link -Werror: %s" % g.stderr.strip()[:160])
    else:
        p = run([os.path.join(TMP, "demo")])
        if p.returncode == 0 and "DEMO OK" in p.stdout:
            ok("mmio: the demo runs, DEMO OK")
        else:
            bad("mmio: the demo exit=%d %s" % (p.returncode, p.stdout.strip()[:120]))

# ---------------------------------------------------------------------------
print("=== 4. volatility ===")
# ---------------------------------------------------------------------------
# A compiler that cached the second rdtsc would return a value that does
# not advance across a million iterations of real work.
code, out = hlc_run(write("vol.hls", VOL_HLS))
if code == 0:
    ok("mmio: a volatile device read is not folded (hosted)")
else:
    bad("mmio: the hosted volatility probe exit=%s (%s)" % (code, out.strip()[:120]))
code, out = hlc_run(write("volfs.hls", VOL_HLS, freestanding=True), freestanding=True)
if code == 0:
    ok("mmio: the same read is not folded under -nostdlib")
else:
    bad("mmio: the freestanding volatility probe exit=%s (%s)" % (code, out.strip()[:120]))

# A 16-byte-aligned stack list must not fault: GCC reaches for `movaps`
# to initialise one, and `movaps` faults on an 8-aligned address — which
# is what a non-naked freestanding entry produced.
code, out = hlc_run(write("aligned.hls", ALIGNED_HLS, freestanding=True),
                    freestanding=True)
if code == 0:
    ok("mmio: a stack-allocated list survives -nostdlib (aligned frame slots)")
else:
    bad("mmio: the stack-list probe exit=%s (%s)" % (code, out.strip()[:120]))

# The entry must be naked: an ordinary C entry lets GCC leave the first
# Halis function 8 bytes off the SysV alignment.
entry = write("entry.hls", "fn main() -> int { return 0 }\n", freestanding=True)
ec = os.path.join(TMP, "entry.fs.c")
run(["./bin/hlc", entry, ec])
if "__attribute__((naked, noreturn)) void _start(void)" in open(
        ec, encoding="utf-8", errors="replace").read():
    ok("mmio: the freestanding entry is naked (ABI stack alignment holds)")
else:
    bad("mmio: the freestanding entry is not naked")

# ---------------------------------------------------------------------------
print("=== 5. the checked constructors ===")
# ---------------------------------------------------------------------------
for name, needle, desc in PANIC_PROGRAMS:
    path = "tests/ok/%s.hls" % name
    if not os.path.exists(path):
        bad("mmio: %s missing" % path)
        continue
    r = run(["python3", "boot/boot.py", "--check", path])
    if r.returncode != 0:
        bad("mmio: %s must be ACCEPTED by the checker" % name)
        continue
    r = run(["python3", "boot/boot.py", path])
    if r.returncode == 101 and needle in (r.stdout + r.stderr):
        ok("mmio: %s panics at run time (%s)" % (name, desc))
    else:
        bad("mmio: %s did not panic: exit=%d" % (name, r.returncode))
    c, out = hlc_run(path)
    if c == 101 and needle in out:
        ok("mmio: %s panics natively too" % name)
    else:
        bad("mmio: %s native exit=%s" % (name, c))

# ---------------------------------------------------------------------------
print("=== 6. behaviour probes ===")
# ---------------------------------------------------------------------------
for name, body, want, desc in PROBES:
    path = write("probe_%s.hls" % name, body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("mmio: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    code, out = hlc_run(path)
    if code != want:
        bad("mmio: probe %s (native) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    code, out = hlc_run(write("probe_%s_fs.hls" % name, body, freestanding=True),
                        freestanding=True)
    if code != want:
        bad("mmio: probe %s (-nostdlib) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    ok("mmio: %s — %s" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 7. the demo + tools ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("mmio: the ok-test is clean on the interpreter")
else:
    bad("mmio: ok-test interpreter exit=%d" % r.returncode)
code, out = hlc_run(OK_TEST)
if code == 0:
    ok("mmio: the ok-test is clean natively")
else:
    bad("mmio: ok-test native exit=%s" % code)
text = open(OK_TEST, encoding="utf-8").read()
# Replace the ATTRIBUTE, not a mention of it in the header comment, and
# do not add a second one.
text = re.sub(r"(?m)^#!\[no_std\]$", "", text, count=1)
code, out = hlc_run(write("ok_fs.hls", text, freestanding=True), freestanding=True)
if code == 0:
    ok("mmio: the ok-test links -nostdlib and exits 0 (freestanding)")
else:
    bad("mmio: the freestanding ok-test exit=%s (%s)" % (code, out.strip()[:120]))

for label, cmd in (
        ("hlfmt -c (ok-test)", ["python3", "tools/hlfmt.py", "-c", OK_TEST]),
        ("hlfmt -c (demo)", ["python3", "tools/hlfmt.py", "-c", DEMO]),
        ("hlfmt -c (core)", ["python3", "tools/hlfmt.py", "-c", CORE]),
        ("hllint (ok-test)", ["python3", "tools/hllint.py", OK_TEST])):
    r = run(cmd)
    if r.returncode == 0:
        ok("mmio: %s" % label)
    else:
        bad("mmio: %s: %s" % (label, (r.stdout + r.stderr).strip()[:140]))

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
