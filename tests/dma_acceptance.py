#!/usr/bin/env python3
"""Stage 89 acceptance gate — core.dma: DMA-safe buffer types.

Run with `make dma-acceptance` (or `python3 tests/dma_acceptance.py`).

Six sections:

  1. core/dma.hls     — the enum / structs / surface, the `core.*`-only
                        import, no effects
  2. the constructors  — the CHECKED ones: a zero length, a misaligned
                        address, an address that does not fit the
                        descriptor's width, a mapping past the window and
                        an over-large bounce transfer all panic
  3. the model         — five behaviour probes on the interpreter AND on
                        both the hosted and `-nostdlib` native paths
  4. the ok-test       — clean on all three paths
  5. the demo + tools  — the demo runs; hlfmt and hllint are clean
  6. the properties    — the six rules a driver actually relies on,
                        asserted explicitly so a later change to the
                        module cannot quietly drop one
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
TMP = tempfile.mkdtemp(prefix="_gate_dma_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage89_dma.hls"
DEMO = "examples/dma_demo.hls"
CORE = "core/dma.hls"

CORE_FNS = [
    "dma_alignment_ok", "dma_is_aligned", "dma_size_ok", "dma_direction_name",
    "dma_needs_flush", "dma_needs_invalidate", "dma_needs_both",
    "dma_addr_bits", "dma_addr_bytes", "dma_addr_ok", "dma_desc_new",
    "dma_desc_end", "dma_desc_covers", "dma_desc_one_page", "dma_desc_pages",
    "dma_page_shift", "dma_window_new", "dma_window_end", "dma_window_has",
    "dma_window_remaining", "dma_map_ok", "dma_map", "dma_unmap",
    "dma_all_unmapped", "bounce_new", "bounce_fits", "bounce_begin",
    "bounce_end", "bounce_cost_bytes",
]

PROBES = [
    ("alignment", """import "core.dma"
fn main() -> int {
    if !dma_alignment_ok(4) || !dma_alignment_ok(4096) { return 1 }
    if dma_alignment_ok(3) || dma_alignment_ok(0) { return 2 }
    if !dma_is_aligned(1048576, 4096) || dma_is_aligned(1048577, 4096) { return 3 }
    if dma_size_ok(0, 1) || !dma_size_ok(4096, 512) { return 4 }
    if dma_size_ok(4097, 512) { return 5 }
    return 0
}
""", 0, "the alignment and size rules a device states, as a testable pair"),
    ("coherency", """import "core.dma"
fn main() -> int {
    if !dma_needs_flush(DmaDirection.ToDevice) { return 1 }
    if dma_needs_invalidate(DmaDirection.ToDevice) { return 2 }
    if dma_needs_flush(DmaDirection.FromDevice) { return 3 }
    if !dma_needs_invalidate(DmaDirection.FromDevice) { return 4 }
    if !dma_needs_both(DmaDirection.Both) { return 5 }
    return 0
}
""", 0, "the flush/invalidate rule each direction follows"),
    ("descriptor", """import "core.dma"
fn main() -> int {
    if !dma_addr_ok(1099511627776, 64) { return 1 }
    if dma_addr_ok(1099511627776, 32) { return 2 }
    let d: DmaDesc = dma_desc_new(1048576, 4096, DmaDirection.ToDevice, 64, 4096)
    if dma_desc_end(d) != 1052672 { return 3 }
    if !dma_desc_covers(d, 1048576, 4096) { return 4 }
    if dma_desc_covers(d, 1048576, 8192) { return 5 }
    if !dma_desc_one_page(d, 12) || dma_desc_pages(d, 12) != 1 { return 6 }
    let s: DmaDesc = dma_desc_new(1048576, 8192, DmaDirection.FromDevice, 64, 4096)
    if dma_desc_one_page(s, 12) { return 7 }
    if dma_desc_pages(s, 12) != 2 { return 8 }
    if dma_page_shift(4096) != 12 { return 9 }
    return 0
}
""", 0, "the descriptor, its address width, and page containment"),
    ("window", """import "core.dma"
fn main() -> int {
    let mut w: DmaWindow = dma_window_new(1048576, 4096, 0)
    if !dma_window_has(w, 1048576) || dma_window_has(w, 1048576 + 4096) { return 1 }
    if !dma_all_unmapped(w) { return 2 }
    w = dma_map(w, 2048)
    if dma_all_unmapped(w) { return 3 }
    w = dma_map(w, 2048)
    if w.mapped != 4096 { return 4 }
    if dma_map_ok(w, 1) { return 5 }
    w = dma_unmap(w, 4096)
    if !dma_all_unmapped(w) { return 6 }
    if dma_unmap(w, 4096).mapped != 0 { return 7 }
    return 0
}
""", 0, "the device-visible window and the map/unmap bookkeeping"),
    ("bounce", """import "core.dma"
fn main() -> int {
    let b: BounceBuf = bounce_new(2097152, 65536, 256)
    if !bounce_fits(b, 65536) || bounce_fits(b, 65537) { return 1 }
    if bounce_fits(b, 0) { return 2 }
    let busy: BounceBuf = bounce_begin(b, 1024)
    if !busy.in_flight { return 3 }
    if bounce_end(busy).in_flight { return 4 }
    if bounce_cost_bytes(1024) != 2048 { return 5 }
    return 0
}
""", 0, "the bounce buffer, its in-flight discipline, and its cost model"),
]

PANIC_PROGRAMS = [
    ("a zero-length DMA descriptor",
     'import "core.dma"\n\nfn main() -> int {\n'
     '    let d: DmaDesc = dma_desc_new(1048576, 0, DmaDirection.ToDevice, 64, 4096)\n'
     '    if d.length != 0 {\n        return 1\n    }\n    return 0\n}\n',
     "a zero-length transfer is a bug"),
    ("a misaligned DMA descriptor",
     'import "core.dma"\n\nfn main() -> int {\n'
     '    let d: DmaDesc = dma_desc_new(1048577, 4096, DmaDirection.ToDevice, 64, 4096)\n'
     '    if d.addr != 1048577 {\n        return 1\n    }\n    return 0\n}\n',
     "meet the device's alignment"),
    ("an address wider than the descriptor",
     'import "core.dma"\n\nfn main() -> int {\n'
     '    let d: DmaDesc = dma_desc_new(1099511627776, 4096, DmaDirection.ToDevice, 32, 4096)\n'
     '    if d.addr_bits != 32 {\n        return 1\n    }\n    return 0\n}\n',
     "the descriptor's width"),
    ("a mapping past the window",
     'import "core.dma"\n\nfn main() -> int {\n'
     '    let w: DmaWindow = dma_window_new(1048576, 4096, 0)\n'
     '    let w2: DmaWindow = dma_map(w, 8192)\n'
     '    if w2.mapped != 8192 {\n        return 1\n    }\n    return 0\n}\n',
     "the end of the window"),
    ("a bounce transfer larger than the buffer",
     'import "core.dma"\n\nfn main() -> int {\n'
     '    let b: BounceBuf = bounce_new(2097152, 4096, 256)\n'
     '    let b2: BounceBuf = bounce_begin(b, 8192)\n'
     '    if b2.length != 4096 {\n        return 1\n    }\n    return 0\n}\n',
     "the transfer does not fit the bounce buffer"),
]

PROPS = [
    ("a descriptor names a PHYSICAL address",
     """import "core.dma"
fn main() -> int {
    let low: DmaDesc = dma_desc_new(1048576, 4096, DmaDirection.ToDevice, 64, 4096)
    if !dma_addr_ok(low.addr, 64) { return 1 }
    # An address a 32-bit descriptor cannot hold, above 4 GiB.
    let high: int = 5497558138880
    if dma_addr_ok(high, 32) { return 2 }
    if !dma_addr_ok(high, 64) { return 3 }
    return 0
}
"""),
    ("a short descriptor is detected, not discovered",
     """import "core.dma"
fn main() -> int {
    let d: DmaDesc = dma_desc_new(1048576, 4096, DmaDirection.ToDevice, 64, 4096)
    if dma_desc_covers(d, 1048576, 8192) { return 1 }
    return 0
}
"""),
    ("an address outside the window is refused",
     """import "core.dma"
fn main() -> int {
    let w: DmaWindow = dma_window_new(1048576, 1048576, 0)
    if dma_window_has(w, 8388608) { return 1 }
    return 0
}
"""),
    ("pages are freed only after the unmap",
     """import "core.dma"
fn main() -> int {
    let mut w: DmaWindow = dma_window_new(1048576, 4096, 0)
    w = dma_map(w, 4096)
    if dma_all_unmapped(w) { return 1 }
    w = dma_unmap(w, 4096)
    if !dma_all_unmapped(w) { return 2 }
    return 0
}
"""),
    ("a second transfer cannot start on a busy bounce buffer",
     """import "core.dma"
fn main() -> int {
    let b: BounceBuf = bounce_new(2097152, 4096, 256)
    let busy: BounceBuf = bounce_begin(b, 1024)
    if !busy.in_flight { return 1 }
    return 0
}
"""),
    ("a from-device transfer invalidates and a to-device one flushes",
     """import "core.dma"
fn main() -> int {
    if !dma_needs_invalidate(DmaDirection.FromDevice) { return 1 }
    if dma_needs_invalidate(DmaDirection.ToDevice) { return 2 }
    if !dma_needs_flush(DmaDirection.ToDevice) { return 3 }
    if dma_needs_flush(DmaDirection.FromDevice) { return 4 }
    return 0
}
"""),
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
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(("#![freestanding]\n" if freestanding else "") + text)
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
print("=== 1. core/dma.hls ===")
# ---------------------------------------------------------------------------
if not os.path.exists(CORE):
    bad("dma: core/dma.hls missing")
else:
    src = open(CORE, encoding="utf-8").read()
    if re.search(r"enum\s+DmaDirection\b", src):
        ok("dma: core.dma declares enum DmaDirection")
    else:
        bad("dma: core.dma missing enum DmaDirection")
    for struct in ("DmaDesc", "DmaWindow", "BounceBuf"):
        if re.search(r"struct\s+%s\b" % struct, src):
            ok("dma: core.dma declares struct %s" % struct)
        else:
            bad("dma: core.dma missing struct %s" % struct)
    imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
    if imports == ["core.result"]:
        ok("dma: core.dma imports only core.result (freestanding-safe)")
    else:
        bad("dma: core.dma imports %r" % imports)
    decls = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    if not re.search(r"^fn[^\n]*\buses\b", decls, re.M) and not re.search(
            r"^\s*extern\b", decls, re.M):
        ok("dma: core.dma declares no effects and no externs")
    else:
        bad("dma: core.dma declares effects or externs")
    for fn in CORE_FNS:
        if re.search(r"fn\s+%s\b" % fn, src):
            ok("dma: core.dma exposes %s" % fn)
        else:
            bad("dma: core.dma missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 2. the constructors are checked ===")
# ---------------------------------------------------------------------------
for desc, body, needle in PANIC_PROGRAMS:
    path = write("panic_%d.hls" % PANIC_PROGRAMS.index((desc, body, needle)), body)
    r = run(["python3", "boot/boot.py", "--check", path])
    if r.returncode != 0:
        bad("dma: %s must be ACCEPTED by the checker" % desc)
        continue
    r = run(["python3", "boot/boot.py", path])
    if r.returncode == 101 and needle in (r.stdout + r.stderr):
        ok("dma: %s panics at run time" % desc)
    else:
        bad("dma: %s did not panic: exit=%d %s"
            % (desc, r.returncode, (r.stdout + r.stderr).strip()[:100]))

# ---------------------------------------------------------------------------
print("=== 3. the model ===")
# ---------------------------------------------------------------------------
for name, body, want, desc in PROBES:
    path = write("probe_%s.hls" % name, body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("dma: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    code, out = hlc_run(path)
    if code != want:
        bad("dma: probe %s (native) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    code, out = hlc_run(write("probe_%s_fs.hls" % name, body, freestanding=True),
                        freestanding=True)
    if code != want:
        bad("dma: probe %s (-nostdlib) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    ok("dma: %s — %s" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 4. the ok-test ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("dma: the ok-test is clean on the interpreter")
else:
    bad("dma: ok-test interpreter exit=%d" % r.returncode)
code, out = hlc_run(OK_TEST)
if code == 0:
    ok("dma: the ok-test is clean natively")
else:
    bad("dma: ok-test native exit=%s (%s)" % (code, out.strip()[:120]))
text = open(OK_TEST, encoding="utf-8").read()
text = re.sub(r"(?m)^#!\[no_std\]$", "", text, count=1)
code, out = hlc_run(write("ok_fs.hls", text, freestanding=True), freestanding=True)
if code == 0:
    ok("dma: the ok-test links -nostdlib and exits 0 (freestanding)")
else:
    bad("dma: the freestanding ok-test exit=%s (%s)" % (code, out.strip()[:120]))

# ---------------------------------------------------------------------------
print("=== 5. the demo + tools ===")
# ---------------------------------------------------------------------------
code, out = hlc_run(DEMO)
if code == 0:
    ok("dma: examples/dma_demo.hls runs, exit 0")
else:
    bad("dma: the demo exit=%s (%s)" % (code, out.strip()[:120]))
for label, cmd in (
        ("hlfmt -c (ok-test)", ["python3", "tools/hlfmt.py", "-c", OK_TEST]),
        ("hlfmt -c (demo)", ["python3", "tools/hlfmt.py", "-c", DEMO]),
        ("hlfmt -c (core)", ["python3", "tools/hlfmt.py", "-c", CORE]),
        ("hllint (ok-test)", ["python3", "tools/hllint.py", OK_TEST])):
    r = run(cmd)
    if r.returncode == 0:
        ok("dma: %s" % label)
    else:
        bad("dma: %s: %s" % (label, (r.stdout + r.stderr).strip()[:140]))

# ---------------------------------------------------------------------------
print("=== 6. the properties a driver relies on ===")
# ---------------------------------------------------------------------------
for i, (desc, body) in enumerate(PROPS):
    path = write("prop_%d.hls" % (i + 1), body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != 0:
        bad("dma: property %r not held" % desc)
        continue
    code, out = hlc_run(path)
    if code != 0:
        bad("dma: property %r not held natively" % desc)
        continue
    ok("dma: %s" % desc)

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
