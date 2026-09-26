#!/usr/bin/env python3
"""Stage 85 acceptance gate — Multiboot2 + Limine boot protocol headers.

Run with `make boot-acceptance` (or `python3 tests/boot_acceptance.py`).

Seven sections, matching the shape of the other OS-dev gates:

  1. crate attributes + guards — `#![boot_header]` parses, takes a bare
                                protocol identifier, is entry-file-only,
                                requires `#![freestanding]`, appears at
                                most once, and every fail program is
                                rejected with the SAME diagnostic by BOTH
                                compilers
  2. core/boot.hls             — the module's enums / structs / surface,
                                its `core.*`-only imports, and no
                                effects
  3. the emitted header        — the Multiboot2 magic, the 40-byte
                                length and the checksum are in the
                                emitted C; the Limine delimiters and
                                request IDs are there; `used` is on the
                                blob (so --gc-sections cannot drop the
                                bytes a firmware scans for)
  4. the LINKED image          — `objdump` shows the header in
                                `.multiboot_header` / `.limine_requests`
                                after a real `-T link.ld` link, and the
                                bytes are the ones the spec says
  5. the Stage 84 integration  — a linker script that does not KEEP the
                                header section is a COMPILE error
                                (the failure this stage exists to stop)
  6. behaviour probes         — the protocol model: the checksum rule,
                                tag walking, both wire encodings of the
                                memory map, reclaimability, the
                                physical-memory-manager questions, the
                                framebuffer, and the boot report
  7. the demos + tools         — boot_demo runs identically on the
                                interpreter and natively, boot_kernel
                                links as a freestanding image,
                                hlfmt/hllint are clean, and `--audit`
                                reports the header on both front-ends
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
# The scratch directory lives INSIDE the repo (one level under tests/)
# so `core.*` imports and the relative `#![link_script("...")]` paths
# resolve the way they do for a real crate: the shipped script is
# "../../link.ld" from here, exactly as it is from tests/fail/.
TMP = tempfile.mkdtemp(prefix="_gate_boot_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage85_boot.hls"
DEMO = "examples/boot_demo.hls"
KERNEL = "examples/boot_kernel.hls"
CORE = "core/boot.hls"

# fail program -> (needle, description)
FAIL_PROGRAMS = [
    ("fail_stage85_boot_protocol", "unknown boot protocol",
     "an unknown protocol name"),
    ("fail_stage85_boot_notident", "expects a protocol name",
     "a string instead of an identifier"),
    ("fail_stage85_boot_hosted", "requires #![freestanding]",
     "a boot header in a hosted crate"),
    ("fail_stage85_boot_dup", "duplicate crate attribute",
     "two #[boot_header] declarations"),
    ("fail_stage85_boot_unplaced", "does not place",
     "a linker script that drops the header section"),
]

# tests/ok/panic_* — the CHECKED constructors. These are accepted by the
# checker and must panic at RUN time.
PANIC_PROGRAMS = [
    ("panic_stage85_memregion", "mem_region_new: length must be >= 0",
     "a negative-length region"),
    ("panic_stage85_fbpitch", "pitch is narrower than one line of pixels",
     "a framebuffer pitch below one line"),
    ("panic_stage85_fbsize", "width and height must be positive",
     "a zero-size framebuffer"),
    ("panic_stage85_findusable", "mem_find_usable: min_len must be positive",
     "a zero-size usable-memory request"),
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


def hlc_run(src):
    """Compile + link + run a HOSTED Halis program; return (exit, output)."""
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


def link_freestanding(src, out, script):
    """Compile a freestanding image and link it the way a bootloader
    would see it: no libc, no PIE, --gc-sections, the repo's script."""
    c = out + ".c"
    r = run(["./bin/hlc", src, c])
    if r.returncode != 0:
        return None, (r.stdout + r.stderr)
    g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-fno-pie",
             "-no-pie", "-ffunction-sections", "-fno-stack-protector",
             "-T", script, "-o", out, c])
    if g.returncode != 0:
        return None, g.stderr
    return out, ""


def normalise(text):
    """Strip the two front-ends' error-kind prefixes so the MESSAGE and
    the line:col are what gets compared."""
    out = re.sub(r"^panic: ", "", text.strip())
    out = re.sub(r"^[a-z ]*error: ", "", out)
    return out


# ---------------------------------------------------------------------------
print("=== 1. crate attributes + guards ===")
# ---------------------------------------------------------------------------
for label, cmd in (("boot", ["python3", "boot/boot.py", "--audit", KERNEL]),
                   ("hlc", ["./bin/hlc", "--audit", KERNEL])):
    r = run(cmd)
    if r.returncode == 0 and "Boot header: limine" in r.stdout:
        ok("boot: #![boot_header] accepted and reported (%s)" % label)
    else:
        bad("boot: #![boot_header] not accepted (%s): %s"
            % (label, (r.stdout + r.stderr).strip()[:120]))

for name, needle, desc in FAIL_PROGRAMS:
    path = "tests/fail/%s.hls" % name
    if not os.path.exists(path):
        bad("boot: %s missing" % path)
        continue
    rb = run(["python3", "boot/boot.py", "--check", path])
    if rb.returncode != 1:
        bad("boot: %s not rejected by boot" % name)
        continue
    rh = run(["./bin/hlc", "--audit", path])
    if rh.returncode == 0:
        bad("boot: %s accepted by hlc (parity)" % name)
        continue
    a = normalise(rb.stdout + rb.stderr)
    b = normalise(rh.stdout + rh.stderr)
    if needle not in a:
        bad("boot: %s rejected without its diagnostic (%s)" % (name, desc))
        continue
    if a != b:
        bad("boot: %s diagnostics differ:\n     boot: %s\n     hlc:  %s"
            % (name, a, b))
        continue
    ok("boot: %s rejected by both compilers (%s)" % (name, desc))

# A crate attribute is an entry-file property, like `#![freestanding]`.
dep = os.path.join(TMP, "bootdep.hls")
with open(dep, "w", encoding="utf-8") as fh:
    fh.write('#![boot_header(limine)]\n\nfn helper() -> int {\n    return 0\n}\n')
entry = os.path.join(TMP, "bootentry.hls")
with open(entry, "w", encoding="utf-8") as fh:
    fh.write('import "./bootdep.hls"\n\nfn main() -> int {\n    return helper()\n}\n')
r = run(["python3", "boot/boot.py", "--check", entry])
if r.returncode == 1 and "only allowed in the entry file" in (r.stdout + r.stderr):
    ok("boot: #![boot_header] is entry-file-only")
else:
    bad("boot: #![boot_header] in a dependency not rejected: %s"
        % (r.stdout + r.stderr).strip()[:120])

# ---------------------------------------------------------------------------
print("=== 2. core/boot.hls ===")
# ---------------------------------------------------------------------------
if not os.path.exists(CORE):
    bad("boot: core/boot.hls missing")
else:
    src = open(CORE, encoding="utf-8").read()
    for enum in ("MemKind", "FbKind"):
        if re.search(r"enum\s+%s\b" % enum, src):
            ok("boot: core.boot declares enum %s" % enum)
        else:
            bad("boot: core.boot missing enum %s" % enum)
    for struct in ("MbTag", "Mb2Header", "LimineFeature", "MemRegion",
                   "Framebuffer"):
        if re.search(r"struct\s+%s\b" % struct, src):
            ok("boot: core.boot declares struct %s" % struct)
        else:
            bad("boot: core.boot missing struct %s" % struct)
    imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
    if imports == ["core.option", "core.result", "core.section"]:
        ok("boot: core.boot imports only core.* (freestanding-safe)")
    else:
        bad("boot: core.boot imports %r" % imports)
    # Look at declarations only — the module's comments discuss effects
    # and externs in prose, and a comment is not a declaration.
    decls = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    if not re.search(r"^fn[^\n]*\buses\b", decls, re.M) and not re.search(
            r"^\s*extern\b", decls, re.M):
        ok("boot: core.boot declares no effects and no externs")
    else:
        bad("boot: core.boot declares effects or externs")
    for fn in ("mb2_magic", "mb2_checksum", "mb2_header_ok",
               "mb2_header_well_formed", "mb2_tag_at", "mb2_tag_name",
               "limine_id_memmap", "limine_id_framebuffer", "limine_id_rsdp",
               "limine_id_hhdm", "limine_id_end", "limine_id_start",
               "limine_base_revision", "limine_base_revision_honoured",
               "limine_id_is", "limine_features_unique",
               "mem_kind_from_e820", "mem_kind_from_limine",
               "mem_kind_reclaimable", "mem_kind_usable_now",
               "mem_usable_bytes", "mem_reclaimable_bytes",
               "mem_largest_usable", "mem_find_usable", "mem_sorted",
               "mem_sort", "mem_no_overlaps", "mem_contains",
               "mem_report", "fb_new", "fb_raw", "fb_geometry_ok",
               "fb_size", "fb_min_pitch", "fb_report", "boot_report"):
        if re.search(r"fn\s+%s\b" % fn, src):
            ok("boot: core.boot exposes %s" % fn)
        else:
            bad("boot: core.boot missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 3. the emitted header ===")
# ---------------------------------------------------------------------------
mb_src = os.path.join(TMP, "mb.hls")
li_src = os.path.join(TMP, "li.hls")
with open(mb_src, "w", encoding="utf-8") as fh:
    fh.write('#![freestanding]\n#![link_script("../../link.ld")]\n'
             '#![boot_header(multiboot2)]\n\nfn main() -> int {\n    return 0\n}\n')
with open(li_src, "w", encoding="utf-8") as fh:
    fh.write('#![freestanding]\n#![link_script("../../link.ld")]\n'
             '#![boot_header(limine)]\n\nfn main() -> int {\n    return 0\n}\n')

for tag, src, want in (("multiboot2", mb_src, [".multiboot_header"]),
                       ("limine", li_src, [".limine_requests"])):
    c = os.path.join(TMP, tag + ".c")
    r = run(["./bin/hlc", src, c])
    if r.returncode != 0:
        bad("boot: %s crate did not compile: %s"
            % (tag, (r.stdout + r.stderr).strip()[:120]))
        continue
    text = open(c, encoding="utf-8", errors="replace").read()
    for sec in want:
        if 'section("%s")' % sec in text:
            ok("boot: the %s header is emitted into %s" % (tag, sec))
        else:
            bad("boot: the %s header is not in %s" % (tag, sec))
    if ", used, aligned(8))" in text:
        ok("boot: the %s header blob is `used` and 8-aligned" % tag)
    else:
        bad("boot: the %s header blob is not used/aligned" % tag)

mb_c = os.path.join(TMP, "multiboot2.c")
text = open(mb_c, encoding="utf-8", errors="replace").read() if os.path.exists(mb_c) else ""
if text:
    for want, why in (("0xE85250D6u", "the magic"),
                      ("0u", "the i386 architecture"),
                      ("40u", "the 40-byte length"),
                      ("397258498u", "the checksum"),
                      ("{ 3u, 16u }", "the module tag"),
                      ("{ 0u, 8u }", "the end tag")):
        if want in text:
            ok("boot: the Multiboot2 header carries %s (%s)" % (why, want))
        else:
            bad("boot: the Multiboot2 header is missing %s (%s)" % (why, want))
    # The spec's invariant, recomputed from the emitted numbers.
    magic = 0xE85250D6
    length = 40
    if (magic + 0 + length + 397258498) % (1 << 32) == 0:
        ok("boot: the emitted header fields sum to zero (the spec's rule)")
    else:
        bad("boot: the emitted checksum does not satisfy the spec")

li_c = os.path.join(TMP, "limine.c")
if os.path.exists(li_c):
    text = open(li_c, encoding="utf-8", errors="replace").read()
    for want, why in (
            ("0xf6b8f4b39de7d1aeUL", "the start delimiter"),
            ("0xadc0e0531bb10d03UL", "the end delimiter"),
            ("0xc7b1dd30df4c8b88UL", "the common request magic"),
            ("0x67cf3d9d378a806fUL", "the memory-map request"),
            ("0x9d5827dcd881dd75UL", "the framebuffer request"),
            ("0xc5e77b6b397e7b43UL", "the RSDP request"),
            ("0x48dcf1cb8ad2b852UL", "the HHDM request"),
            ("0x224ef0460a8e8926UL", "the stack-size request")):
        if want in text:
            ok("boot: the Limine request list carries %s" % why)
        else:
            bad("boot: the Limine request list is missing %s (%s)" % (why, want))
    if "UL" in text and "0xc7b1dd30df4c8b88" in text:
        ok("boot: Limine IDs are unsigned literals (half are above 2^63)")

# ---------------------------------------------------------------------------
print("=== 4. the LINKED image ===")
# ---------------------------------------------------------------------------
if shutil.which("objdump") is None:
    bad("boot: objdump not available — cannot verify the linked image")
else:
    for tag, section, magic_bytes in (
            ("multiboot2", ".multiboot_header", "d65052e8"),
            ("limine", ".limine_requests", "aed1e79d")):
        elf = os.path.join(TMP, tag + ".elf")
        out, err = link_freestanding(
            mb_src if tag == "multiboot2" else li_src, elf, "link.ld")
        if out is None:
            bad("boot: the %s image did not link: %s" % (tag, err.strip()[:120]))
            continue
        hdr = run(["objdump", "-h", elf]).stdout
        if re.search(r"\s%s\b" % re.escape(section), hdr):
            ok("boot: the linked %s image has a %s section" % (tag, section))
        else:
            bad("boot: the linked %s image has no %s" % (tag, section))
        dump = run(["objdump", "-s", "-j", section, elf]).stdout
        if magic_bytes in dump:
            ok("boot: %s starts with the protocol's own bytes" % section)
        else:
            bad("boot: %s does not start with %s" % (section, magic_bytes))
        # objdump's section table ends each line with the alignment as
        # a power of two: 2**3 is 8 bytes.
        line = ""
        for l in hdr.split("\n"):
            if section in l:
                line = l
                break
        if line.rstrip().endswith("2**3"):
            ok("boot: %s is 8-byte aligned" % section)
        else:
            bad("boot: %s is not 8-byte aligned (%r)" % (section, line.strip()))

# ---------------------------------------------------------------------------
print("=== 5. the Stage 84 integration ===")
# ---------------------------------------------------------------------------
# The shipped link.ld must place BOTH header sections, or a
# `#![boot_header]` crate is a compile error. This is the check that
# turns "the bootloader refused to boot my kernel" into a build error.
script = open("link.ld", encoding="utf-8").read()
for sec in (".multiboot_header", ".limine_requests"):
    # The section must be NAMED and its input KEPT — a bare
    # `*(.multiboot_header)` without a KEEP is exactly the silent
    # failure the compiler now rejects.
    if re.search(r"^\s*%s\b" % re.escape(sec), script, re.M) and \
            re.search(r"KEEP\(\*\(%s\)\)" % re.escape(sec), script):
        ok("boot: link.ld KEEPs %s" % sec)
    else:
        bad("boot: link.ld does not KEEP %s" % sec)

# A script WITHOUT the header section is rejected at compile time.
nobootscript = os.path.join(TMP, "no_boot.ld")
stripped = re.sub(r"    /\* Stage 85.*?KEEP\(\*\(\.limine_requests\)\)\n    \}\n\n",
                  "", script, flags=re.S)
with open(nobootscript, "w", encoding="utf-8") as fh:
    fh.write(stripped)
probe = os.path.join(TMP, "unplaced.hls")
with open(probe, "w", encoding="utf-8") as fh:
    fh.write('#![freestanding]\n#![link_script("no_boot.ld")]\n'
             '#![boot_header(multiboot2)]\n\nfn main() -> int {\n    return 0\n}\n')
rb = run(["python3", "boot/boot.py", "--check", probe])
rh = run(["./bin/hlc", "--audit", probe])
if rb.returncode == 1 and "does not place" in (rb.stdout + rb.stderr) \
        and rh.returncode != 0 and "does not place" in (rh.stdout + rh.stderr):
    ok("boot: a script that drops the header section is a compile error")
else:
    bad("boot: an unplaced header was not rejected: %s" % (rb.stdout + rb.stderr)[:120])

# ...and the same crate against the SHIPPED script is accepted.
probe2 = os.path.join(TMP, "placed.hls")
with open(probe2, "w", encoding="utf-8") as fh:
    fh.write('#![freestanding]\n#![link_script("../../link.ld")]\n'
             '#![boot_header(multiboot2)]\n'
             '#[section(".text.extra")]\nfn extra(x: int) -> int {\n    return x\n}\n\n'
             'fn main() -> int {\n    return extra(0)\n}\n')
r = run(["./bin/hlc", probe2, os.path.join(TMP, "placed.c")])
if r.returncode == 0:
    ok("boot: the same crate is accepted under the shipped link.ld")
else:
    bad("boot: a placed header was rejected: %s" % (r.stdout + r.stderr)[:140])

# ---------------------------------------------------------------------------
print("=== 6. behaviour probes ===")
# ---------------------------------------------------------------------------
PROBES = [
    ("checksum", '''
import "core.boot"
import "core.section"
fn main() -> int {
    # The spec's rule: the four header fields sum to zero, modulo 2^32.
    if mb2_checksum(mb2_magic(), 0, 40) != 397258498 { return 1 }
    if mb2_sum32(mb2_magic(), 0, 40, mb2_checksum(mb2_magic(), 0, 40)) != 0 { return 2 }
    if mb2_field32(-1) != 4294967295 { return 3 }
    if mb2_field32(4294967296) != 0 { return 4 }
    if mb2_header_ok(mb2_magic(), 0, 24, mb2_checksum(mb2_magic(), 0, 24)) != true { return 5 }
    if mb2_header_ok(mb2_magic(), 0, 24, 0) != false { return 6 }
    if mb2_header_ok(mb2_magic() + 1, 0, 24, 0) != false { return 7 }
    return 0
}
''', 0, "the Multiboot2 checksum rule, in 32-bit arithmetic"),

    ("headers", '''
import "core.boot"
fn main() -> int {
    let h: Mb2Header = mb2_header_new([])
    if h.length != 24 { return 1 }
    if !mb2_header_well_formed(h) { return 2 }
    if mb2_tag_present(h, mb2_tag_module()) { return 3 }
    if mb2_tag_at(h, mb2_tag_end(), 0) != 0 { return 4 }
    let mut tags: list[MbTag] = []
    tags.push(mb_tag_new(mb2_tag_cmdline(), [65, 0]))
    let h2: Mb2Header = mb2_header_new(tags)
    if !mb2_header_well_formed(h2) { return 5 }
    if mb2_tag_count(h2, mb2_tag_cmdline()) != 1 { return 6 }
    if mb_tag_payload_ok(MbTag{tag_type: 4, size: 4, payload: []}) { return 7 }
    return 0
}
''', 0, "header construction, the mandatory end tag, and tag lookup"),

    ("memmap-encodings", '''
import "core.boot"
fn main() -> int {
    # E820 numbers usable as 1; Limine numbers it 0. One model, two
    # wire encodings.
    if mem_kind_id(mem_kind_from_e820(1)) != mem_kind_id(mem_kind_from_limine(0)) { return 1 }
    if mem_kind_id(mem_kind_from_e820(2)) != mem_kind_id(mem_kind_from_limine(1)) { return 2 }
    if mem_kind_id(mem_kind_from_e820(5)) != mem_kind_id(mem_kind_from_limine(4)) { return 3 }
    if mem_kind_id(mem_kind_from_limine(5)) != mem_kind_id(MemKind.BootloaderReclaimable) { return 4 }
    if mem_kind_id(mem_kind_from_e820(6)) != mem_kind_id(MemKind.Unknown) { return 5 }
    if mem_kind_code_e820(MemKind.Framebuffer) != -1 { return 6 }
    return 0
}
''', 0, "E820 and Limine wire encodings land in one model"),

    ("reclaim", '''
import "core.boot"
fn main() -> int {
    # The loader's scratch is reclaimable but NOT usable now: taking it
    # on day one overwrites the page tables the kernel is running on.
    if !mem_kind_reclaimable(MemKind.BootloaderReclaimable) { return 1 }
    if mem_kind_usable_now(MemKind.BootloaderReclaimable) { return 2 }
    if !mem_kind_reclaimable(MemKind.AcpiReclaimable) { return 3 }
    if mem_kind_reclaimable(MemKind.AcpiNvs) { return 4 }
    if mem_kind_reclaimable(MemKind.BadMemory) { return 5 }
    if mem_kind_reclaimable(MemKind.Unknown) { return 6 }
    if !mem_kind_usable_now(MemKind.Usable) { return 7 }
    return 0
}
''', 0, "reclaimability is ordered, and an unknown type is never free"),

    ("pmm", '''
import "core.boot"
fn main() -> int {
    let mut rs: list[MemRegion] = []
    rs.push(mem_region_new(0, 4096, MemKind.Reserved))
    rs.push(mem_region_new(4096, 65536, MemKind.Usable))
    rs.push(mem_region_new(1048576, 1048576, MemKind.Usable))
    rs.push(mem_region_new(4194304, 8192, MemKind.BootloaderReclaimable))
    if mem_usable_bytes(rs) != 1114112 { return 1 }
    if mem_reclaimable_bytes(rs) != 1122304 { return 2 }
    if mem_largest_usable(rs) != 1048576 { return 3 }
    if mem_count_usable(rs) != 2 { return 4 }
    if mem_find_usable(rs, 65536, 4096) != 4096 { return 5 }
    if mem_find_usable(rs, 65537, 4096) != 1048576 { return 6 }
    if mem_find_usable(rs, 1099511627776, 4096) != -1 { return 7 }
    if mem_contains(rs, 1048676) != 2 { return 8 }
    if mem_contains(rs, 1000000) != -1 { return 9 }
    return 0
}
''', 0, "the day-one questions: totals, the largest region, first frame"),

    ("consistency", '''
import "core.boot"
fn main() -> int {
    let mut rs: list[MemRegion] = []
    rs.push(mem_region_new(0, 4096, MemKind.Reserved))
    rs.push(mem_region_new(4096, 65536, MemKind.Usable))
    rs.push(mem_region_new(1048576, 1048576, MemKind.Usable))
    if !mem_sorted(rs) { return 1 }
    if !mem_no_overlaps(rs) { return 2 }
    let mut sh: list[MemRegion] = []
    sh.push(rs.get(2))
    sh.push(rs.get(0))
    if mem_sorted(sh) { return 3 }
    if !mem_sorted(mem_sort(sh)) { return 4 }
    let mut clash: list[MemRegion] = []
    clash.push(mem_region_new(4096, 8192, MemKind.Usable))
    clash.push(mem_region_new(4096 + 4096, 8192, MemKind.Usable))
    if mem_no_overlaps(clash) { return 5 }
    let mut mixed: list[MemRegion] = []
    mixed.push(mem_region_new(4096, 8192, MemKind.Usable))
    mixed.push(mem_region_new(4096 + 4096, 8192, MemKind.Framebuffer))
    if !mem_no_overlaps(mixed) { return 6 }
    return 0
}
''', 0, "a map that is unsorted or double-counts is reported, not used"),

    ("framebuffer", '''
import "core.boot"
fn main() -> int {
    let fb: Framebuffer = fb_new(1073741824, 640, 480, 4096, 32, FbKind.Rgb)
    if fb_size(fb) != 4096 * 480 { return 1 }
    if fb_min_pitch(fb) != 2560 { return 2 }
    if !fb_geometry_ok(fb) { return 3 }
    # firmware data goes through fb_raw, so a parser never panics
    if fb_geometry_ok(fb_raw(0, 640, 480, 4096, 32, FbKind.Rgb)) { return 4 }
    if fb_geometry_ok(fb_raw(1073741824, 640, 480, 2048, 32, FbKind.Rgb)) { return 5 }
    if fb_geometry_ok(fb_raw(1073741824, 640, 480, 4096, 15, FbKind.Rgb)) { return 6 }
    return 0
}
''', 0, "the pitch is the firmware's, and a bad mode is a value to reject"),

    ("limine-ids", '''
import "core.boot"
import "core.section"
fn main() -> int {
    let m: list[int] = limine_id_memmap()
    if m.len() != 4 { return 1 }
    # IDs are unsigned 64-bit words; in an int they arrive negative for
    # more than half of them, so hex64 must render the pattern.
    if hex64(m.get(3)) != "0xe304acdfc50c3c62" { return 2 }
    if hex64(m.get(2)) != "0x67cf3d9d378a806f" { return 3 }
    if !limine_id_is(m, limine_id_memmap()) { return 4 }
    if limine_id_is([m.get(0), m.get(1), m.get(2), 0], limine_id_memmap()) { return 5 }
    if limine_id_name(limine_id_rsdp()) != "rsdp" { return 6 }
    if limine_id_name([1, 2, 3, 4]) != "" { return 7 }
    let rev: list[int] = limine_base_revision(3)
    rev.set(2, 0)
    if !limine_base_revision_honoured(rev) { return 8 }
    if limine_base_revision_honoured(limine_base_revision(3)) { return 9 }
    return 0
}
''', 0, "request IDs are compared exactly and rendered unsigned"),

    ("report", '''
import "core.boot"
fn main() -> int {
    let mut rs: list[MemRegion] = []
    rs.push(mem_region_new(0, 4096, MemKind.Reserved))
    rs.push(mem_region_new(4096, 65536, MemKind.Usable))
    let fb: Framebuffer = fb_new(1073741824, 640, 480, 4096, 32, FbKind.Rgb)
    let r: str = boot_report("limine", rs, fb, true)
    if r.find("1 usable region") < 0 { return 1 }
    if r.find("640x480") < 0 { return 2 }
    if boot_report("limine", rs, fb, false).find("framebuffer: none") < 0 { return 3 }
    let mut none: list[MemRegion] = []
    none.push(mem_region_new(0, 4096, MemKind.Reserved))
    if boot_report("limine", none, fb, true).find("no usable memory") < 0 { return 4 }
    return 0
}
''', 0, "the boot report names the protocol and warns about a bad map"),
]

for name, body, want, desc in PROBES:
    path = os.path.join(TMP, "probe_%s.hls" % name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("boot: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    code, out = hlc_run(path)
    if code != want:
        bad("boot: probe %s (native) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    ok("boot: %s — %s" % (name, desc))

# The checked constructors must panic at RUN time (accepted by the
# checker, loud at boot).
for name, needle, desc in PANIC_PROGRAMS:
    path = "tests/ok/%s.hls" % name
    if not os.path.exists(path):
        bad("boot: %s missing" % path)
        continue
    r = run(["python3", "boot/boot.py", "--check", path])
    if r.returncode != 0:
        bad("boot: %s must be ACCEPTED by the checker" % name)
        continue
    r = run(["python3", "boot/boot.py", path])
    if r.returncode == 101 and needle in (r.stdout + r.stderr):
        ok("boot: %s panics at run time (%s)" % (name, desc))
    else:
        bad("boot: %s did not panic: exit=%d" % (name, r.returncode))
    code, out = hlc_run(path)
    if code == 101 and needle in out:
        ok("boot: %s panics natively too" % name)
    else:
        bad("boot: %s native exit=%s" % (name, code))

# ---------------------------------------------------------------------------
print("=== 7. the demos + tools ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("boot: the ok-test is clean on the interpreter")
else:
    bad("boot: ok-test interpreter exit=%d" % r.returncode)
code, out = hlc_run(OK_TEST)
if code == 0:
    ok("boot: the ok-test is clean natively")
else:
    bad("boot: ok-test native exit=%s" % code)
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
        ok("boot: the ok-test links -nostdlib and exits 0 (freestanding)")
    else:
        bad("boot: the freestanding ok-test failed: %s" % g.stderr.strip()[:120])
else:
    bad("boot: the ok-test did not compile as freestanding")

# boot_demo: identical output on the interpreter and natively.
ri = run(["python3", "boot/boot.py", DEMO])
code, out = hlc_run(DEMO)
if ri.returncode == 0 and code == 0 and "DEMO OK" in out:
    ok("boot: boot_demo runs on both, DEMO OK")
else:
    bad("boot: boot_demo interp=%s native=%s" % (ri.returncode, code))
if ri.stdout == out:
    ok("boot: boot_demo output is byte-identical (differential)")
else:
    bad("boot: boot_demo differs between interpreter and native")

# boot_kernel: the real freestanding image.
elf = os.path.join(TMP, "kernel.elf")
out, err = link_freestanding(KERNEL, elf, "link.ld")
if out is None:
    bad("boot: boot_kernel did not link: %s" % err.strip()[:140])
else:
    hdr = run(["objdump", "-h", elf]).stdout
    if ".limine_requests" in hdr:
        ok("boot: the kernel image carries .limine_requests")
    else:
        bad("boot: the kernel image has no .limine_requests")
    if "void _start" in open(elf + ".c", encoding="utf-8", errors="replace").read():
        ok("boot: the kernel image enters at _start (no libc)")
    else:
        bad("boot: the kernel image has no _start")

# Tools.
for label, cmd, want in (
        ("hlfmt -c (ok-test)", ["python3", "tools/hlfmt.py", "-c", OK_TEST], 0),
        ("hlfmt -c (demo)", ["python3", "tools/hlfmt.py", "-c", DEMO], 0),
        ("hlfmt -c (kernel)", ["python3", "tools/hlfmt.py", "-c", KERNEL], 0),
        ("hlfmt -c (core)", ["python3", "tools/hlfmt.py", "-c", CORE], 0),
        ("hllint (ok-test)", ["python3", "tools/hllint.py", OK_TEST], 0)):
    r = run(cmd)
    if r.returncode == want:
        ok("boot: %s" % label)
    else:
        bad("boot: %s: %s" % (label, (r.stdout + r.stderr).strip()[:140]))

# --audit parity on the lines this stage adds.
for label, cmd in (("boot", ["python3", "boot/boot.py", "--audit", KERNEL]),
                   ("hlc", ["./bin/hlc", "--audit", KERNEL])):
    r = run(cmd)
    text = r.stdout + r.stderr
    if r.returncode == 0 and "Boot header: limine (emitted into .limine_requests)" in text:
        ok("boot: --audit names the header and its section (%s)" % label)
    else:
        bad("boot: --audit missing the boot header (%s): %s"
            % (label, text.strip()[:140]))
if "Boot header: multiboot2 (emitted into .multiboot_header)" in run(
        ["./bin/hlc", "--audit", mb_src]).stdout:
    ok("boot: --audit names the Multiboot2 section too")
else:
    bad("boot: --audit does not name the Multiboot2 section")

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
