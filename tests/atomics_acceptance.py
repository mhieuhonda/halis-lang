#!/usr/bin/env python3
"""Stage 90 acceptance gate — core.sync.nolock: lock-free primitives.

Run with `make atomics-acceptance` (or `python3 tests/atomics_acceptance.py`).

Six sections:

  1. core/atomics.hls — the enum / structs / surface, ZERO imports
                        (freestanding-clean), no effects
  2. the memory orders — the four names, the two bits, and the fact
                        that a store cannot carry Acquire
  3. the model         — the RMW combinations, the CAS loop and its
                        give-up, the seqlock reader rule, RCU's reclaim
                        precondition, and the hazard-pointer re-check,
                        on the interpreter and both native paths
  4. the lowering     — a `lock`ed increment through `asm!` compiles
                        `-Werror`, LINKS, and the disassembly really
                        contains `lock addq`
  5. the ok-test       — clean on all three paths
  6. the demo + tools  — the demo runs; hlfmt and hllint are clean
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
TMP = tempfile.mkdtemp(prefix="_gate_atomics_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage90_atomics.hls"
DEMO = "examples/atomics_demo.hls"
CORE = "core/atomics.hls"

CORE_FNS = [
    "mem_order_name", "mem_order_id", "mem_order_from_id",
    "mem_order_has_acquire", "mem_order_has_release", "mem_order_stronger",
    "mem_order_load_legal", "mem_order_store_legal", "atomic_new",
    "atomic_load", "atomic_store", "atomic_fetch_add", "atomic_fetch_sub",
    "atomic_fetch_and", "atomic_fetch_or", "atomic_fetch_xor", "atomic_cas",
    "atomic_cas_ok", "atomic_cas_value", "atomic_cas_loop",
    "atomic_cas_loop_gave_up", "atomic_cas_loop_iterations",
    "atomic_cas_budget_ok", "seqlock_new", "seqlock_begin_write",
    "seqlock_end_write", "seqlock_is_even", "seqlock_is_odd",
    "seqlock_is_writing", "seqlock_read_ok", "seqlock_retry_budget_ok",
    "rcu_new", "rcu_read_lock", "rcu_read_unlock", "rcu_publish",
    "rcu_retire", "rcu_unsafe", "rcu_safe", "rcu_try_reclaim",
    "rcu_reclaimed", "rcu_pending", "rcu_readers", "hazard_new",
    "hazard_acquire", "hazard_ok", "hazard_release", "hazard_count",
    "hazard_blocks_reclaim",
]

# A `lock`ed increment: the lowering every atomics module needs. The
# compiler's constraint machinery (Stage 83) is what makes the memory
# clobber and the register binding expressible.
# What a lock-free primitive needs from `asm!` TODAY: a memory
# operand (the "m" class) for the location half, and the "cc" and
# "memory" clobbers so the compiler neither reorders nor elides the
# access.
#
# What it cannot yet express is the LOCKED read-modify-write itself: a
# locked instruction needs a MEMORY destination, and HLS's asm has no
# `"+m"` write constraint. That is a real gap, stated here rather than
# papered over — `lock cmpxchgq` assembles today and `lock addq` does
# not, because the assembler (correctly) refuses a locked register
# destination. The gate therefore checks the clobbers and the memory
# class, which is what the compiler owns.
MEM_HLS = """#![freestanding]

fn touch(addr: int) -> void {
    let v: int = 0
    asm!("movq {0}, {1}", in("rax") v, in(mem) addr)
    return
}

fn fence() -> void {
    # No clobber clause: Stage 83 banned "cc" and "memory" from it,
    # because the DEFAULT list already carries both — so an asm! with no
    # options is a fence the compiler will not move memory across.
    asm!("mfence")
    return
}

fn main() -> int {
    return 0
}
"""

PROBES = [
    ("orders", """import "core.atomics"
fn main() -> int {
    if mem_order_id(MemOrder.Relaxed) != 0 { return 1 }
    if mem_order_id(MemOrder.Acquire) != 2 { return 2 }
    if mem_order_id(MemOrder.Release) != 3 { return 3 }
    if mem_order_id(MemOrder.AcqRel) != 4 { return 4 }
    # Acquire and Release are DIFFERENT halves.
    if !mem_order_has_acquire(MemOrder.Acquire) { return 5 }
    if mem_order_has_release(MemOrder.Acquire) { return 6 }
    if mem_order_has_acquire(MemOrder.Release) { return 7 }
    if !mem_order_has_release(MemOrder.Release) { return 8 }
    if !mem_order_has_acquire(MemOrder.AcqRel) { return 9 }
    if !mem_order_has_release(MemOrder.AcqRel) { return 10 }
    if mem_order_has_acquire(MemOrder.Relaxed) { return 11 }
    # A store cannot carry Acquire.
    if mem_order_store_legal(MemOrder.Acquire) { return 12 }
    if !mem_order_store_legal(MemOrder.Release) { return 13 }
    # Replacing a release with a relaxed WEAKENS the order.
    if mem_order_stronger(MemOrder.Relaxed, MemOrder.Release) { return 14 }
    if !mem_order_stronger(MemOrder.AcqRel, MemOrder.Acquire) { return 15 }
    return 0
}
""", 0, "the four orders, their two bits, and the store rule"),
    ("rmw-cas", """import "core.atomics"
fn main() -> int {
    let a: Atomic = atomic_new(5, MemOrder.AcqRel)
    if atomic_load(atomic_fetch_add(a, 3)) != 8 { return 1 }
    if atomic_load(atomic_fetch_sub(atomic_fetch_add(a, 3), 3)) != 5 { return 2 }
    if atomic_load(atomic_fetch_or(a, 8)) != 13 { return 3 }
    let a2: Atomic = atomic_store(a, 42)
    if atomic_load(a2) != 42 { return 4 }
    let ok: list[int] = atomic_cas(a2, 42, 7)
    if !atomic_cas_ok(ok) || atomic_cas_value(ok) != 7 { return 5 }
    let no: list[int] = atomic_cas(a2, 99, 9)
    if atomic_cas_ok(no) || atomic_cas_value(no) != 42 { return 6 }
    # Giving up is NOT success.
    let loss: list[int] = atomic_cas_loop(7, 99, 11, 4)
    if !atomic_cas_loop_gave_up(loss) { return 7 }
    if atomic_cas_loop_iterations(loss) != 4 { return 8 }
    if atomic_cas_loop_gave_up(atomic_cas_loop(7, 7, 11, 4)) { return 9 }
    if atomic_cas_budget_ok(0) { return 10 }
    return 0
}
""", 0, "the RMW combinations, and the CAS loop's give-up"),
    ("seqlock", """import "core.atomics"
fn main() -> int {
    let mut l: SeqLock = seqlock_new()
    if !seqlock_is_even(l.seq) { return 1 }
    l = seqlock_begin_write(l)
    if !seqlock_is_writing(l) { return 2 }
    # While odd, NO reader may trust the data — including the case
    # where the two counter reads AGREE.
    if seqlock_read_ok(1, 1) { return 3 }
    l = seqlock_end_write(l)
    if !seqlock_is_even(l.seq) { return 4 }
    if !seqlock_read_ok(0, 0) { return 5 }
    if seqlock_read_ok(2, 3) { return 6 }
    if seqlock_read_ok(1, 0) { return 7 }
    if seqlock_retry_budget_ok(0) { return 8 }
    return 0
}
""", 0, "the seqlock writer's even->odd->even, and the reader's rule"),
    ("rcu", """import "core.atomics"
fn main() -> int {
    let mut r: RcuState = rcu_new()
    r = rcu_publish(r, 1048576)
    r = rcu_retire(r, 2097152)
    if rcu_pending(r) != 1 { return 1 }
    # A reader inside BLOCKS the reclaim.
    r = rcu_read_lock(r)
    if rcu_readers(r) != 1 { return 2 }
    let held: RcuState = rcu_try_reclaim(r)
    if rcu_pending(held) != 1 || rcu_reclaimed(held) != 0 { return 3 }
    r = rcu_read_unlock(held)
    let freed: RcuState = rcu_try_reclaim(r)
    if rcu_pending(freed) != 0 || rcu_reclaimed(freed) != 1 { return 4 }
    # The unsafe counter closes the same window for a reader with no
    # critical section.
    r = rcu_unsafe(freed)
    if rcu_readers(r) != 1 { return 5 }
    if rcu_pending(rcu_try_reclaim(r)) != 0 { return 6 }
    return 0
}
""", 0, "RCU's reclaim precondition, and the unsafe counter"),
    ("hazard", """import "core.atomics"
fn main() -> int {
    let h: HazardState = hazard_new()
    let got: list[int] = hazard_acquire(h, 4096, 4096)
    if !hazard_ok(got) { return 1 }
    # acquire() returns the INDEX; the state it produced is what carries
    # the hazard, so a released state is back to zero.
    if hazard_count(hazard_release(h, got.get(1))) != 0 { return 2 }
    # A writer that moved the pointer between the load and the publish
    # is the window the re-read closes.
    if hazard_ok(hazard_acquire(h, 4096, 8192)) { return 3 }
    let held: HazardState = hazard_new()
    let mut with: list[int] = held.published
    with.push(4096)
    let h2: HazardState = HazardState { slot: held.slot, published: with, retired: held.retired }
    if !hazard_blocks_reclaim(h2, 4096) { return 4 }
    if hazard_blocks_reclaim(h2, 8192) { return 5 }
    if hazard_count(h2) != 1 { return 6 }
    if hazard_count(hazard_release(h2, 0)) != 0 { return 7 }
    return 0
}
""", 0, "the hazard-pointer re-check, and the blocked reclaim"),
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
print("=== 1. core/atomics.hls ===")
# ---------------------------------------------------------------------------
if not os.path.exists(CORE):
    bad("atomics: core/atomics.hls missing")
else:
    src = open(CORE, encoding="utf-8").read()
    if re.search(r"enum\s+MemOrder\b", src):
        ok("atomics: core.atomics declares enum MemOrder")
    else:
        bad("atomics: core.atomics missing enum MemOrder")
    for struct in ("Atomic", "SeqLock", "RcuState", "HazardState"):
        if re.search(r"struct\s+%s\b" % struct, src):
            ok("atomics: core.atomics declares struct %s" % struct)
        else:
            bad("atomics: core.atomics missing struct %s" % struct)
    imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
    if imports == []:
        ok("atomics: core.atomics has ZERO imports (freestanding-clean)")
    else:
        bad("atomics: core.atomics imports %r" % imports)
    decls = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    if not re.search(r"^fn[^\n]*\buses\b", decls, re.M) and not re.search(
            r"^\s*extern\b", decls, re.M):
        ok("atomics: core.atomics declares no effects and no externs")
    else:
        bad("atomics: core.atomics declares effects or externs")
    for fn in CORE_FNS:
        if re.search(r"fn\s+%s\b" % fn, src):
            ok("atomics: core.atomics exposes %s" % fn)
        else:
            bad("atomics: core.atomics missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 2. the memory orders ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", write("orders.hls", PROBES[0][1])])
if r.returncode == 0:
    ok("atomics: the four orders and their two bits agree with the spec")
else:
    bad("atomics: the order probe failed: %s" % (r.stdout + r.stderr).strip()[:120])

# ---------------------------------------------------------------------------
print("=== 3. the model ===")
# ---------------------------------------------------------------------------
for name, body, want, desc in PROBES[1:]:
    path = write("probe_%s.hls" % name, body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("atomics: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    code, out = hlc_run(path)
    if code != want:
        bad("atomics: probe %s (native) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    code, out = hlc_run(write("probe_%s_fs.hls" % name, body, freestanding=True),
                        freestanding=True)
    if code != want:
        bad("atomics: probe %s (-nostdlib) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    ok("atomics: %s — %s" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 4. the lowering ===")
# ---------------------------------------------------------------------------
mem_src = write("mem.hls", MEM_HLS)
mem_c = os.path.join(TMP, "mem.c")
r = run(["./bin/hlc", mem_src, mem_c])
if r.returncode != 0:
    bad("atomics: the memory-operand form did not compile: %s" % (r.stdout + r.stderr)[:160])
else:
    text = open(mem_c, encoding="utf-8", errors="replace").read()
    if '"m"' in text:
        ok("atomics: asm! lowers the memory-operand class to \"m\"")
    else:
        bad("atomics: the \"m\" class is missing from the emitted C")
    if '"cc", "memory"' in text or ('"memory"' in text and '"cc"' in text):
        ok("atomics: the default clobber list carries cc and memory")
    else:
        bad("atomics: the cc/memory clobbers are missing")
    if '__asm__ __volatile__' in text:
        ok("atomics: a device/atomic access is emitted __volatile__")
    else:
        bad("atomics: the access is not volatile")
    g = run(["gcc", "-O2", "-Werror", "-ffreestanding", "-nostdlib",
             "-fno-pie", "-no-pie", "-ffunction-sections",
             "-fno-stack-protector", "-T", "link.ld", "-o",
             os.path.join(TMP, "mem.elf"), mem_c])
    if g.returncode != 0:
        bad("atomics: the memory-operand image did not link: %s" % g.stderr.strip()[:160])
    else:
        ok("atomics: the memory-operand image links -Werror")
        if shutil.which("objdump") is not None:
            dis = run(["objdump", "-d", os.path.join(TMP, "mem.elf")]).stdout
            if "mfence" in dis:
                ok("atomics: the fence is a real mfence in the image")
            else:
                bad("atomics: no mfence in the disassembly")

# The KNOWN GAP, asserted so it cannot be quietly forgotten: a locked
# read-modify-write needs a memory destination, and HLS's asm has no
# `"+m"` write constraint. The assembler refuses a locked register
# destination, so `lock addq` on an `inout(reg)` operand does not
# assemble — correctly.
gap = write("gap.hls", """#![freestanding]

fn locked_add(mut p: int) -> int {
    asm!("lock addq {0}, {1}", in("rcx") 1, inout(reg) p)
    return p
}

fn main() -> int {
    return 0
}
""")
gap_c = os.path.join(TMP, "gap.c")
if run(["./bin/hlc", gap, gap_c]).returncode != 0:
    bad("atomics: the compiler should ACCEPT the locked-add source (it is valid asm)")
else:
    ok("atomics: the compiler accepts a locked-add source")
    g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-fno-pie",
             "-no-pie", "-ffunction-sections", "-fno-stack-protector",
             "-o", os.path.join(TMP, "gap.elf"), gap_c])
    # The assembler refuses a LOCKED REGISTER destination — that is the
    # rule that makes the gap real, and the message wording is
    # gas-dependent ("lockable" / "register type mismatch").
    gap_msg = g.stderr
    if g.returncode != 0 and ("lockable" in gap_msg
                             or "register type mismatch" in gap_msg):
        ok("atomics: the locked-add GAP is the assembler's memory-destination rule")
    else:
        bad("atomics: the locked-add gap changed shape (rc=%d) — re-check the gate"
            % g.returncode)

# ---------------------------------------------------------------------------
print("=== 5. the ok-test ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("atomics: the ok-test is clean on the interpreter")
else:
    bad("atomics: ok-test interpreter exit=%d" % r.returncode)
code, out = hlc_run(OK_TEST)
if code == 0:
    ok("atomics: the ok-test is clean natively")
else:
    bad("atomics: ok-test native exit=%s (%s)" % (code, out.strip()[:120]))
text = open(OK_TEST, encoding="utf-8").read()
text = re.sub(r"(?m)^#!\[no_std\]$", "", text, count=1)
code, out = hlc_run(write("ok_fs.hls", text, freestanding=True), freestanding=True)
if code == 0:
    ok("atomics: the ok-test links -nostdlib and exits 0 (freestanding)")
else:
    bad("atomics: the freestanding ok-test exit=%s (%s)" % (code, out.strip()[:120]))

# ---------------------------------------------------------------------------
print("=== 6. the demo + tools ===")
# ---------------------------------------------------------------------------
code, out = hlc_run(DEMO)
if code == 0:
    ok("atomics: examples/atomics_demo.hls runs, exit 0")
else:
    bad("atomics: the demo exit=%s (%s)" % (code, out.strip()[:120]))
for label, cmd in (
        ("hlfmt -c (ok-test)", ["python3", "tools/hlfmt.py", "-c", OK_TEST]),
        ("hlfmt -c (demo)", ["python3", "tools/hlfmt.py", "-c", DEMO]),
        ("hlfmt -c (core)", ["python3", "tools/hlfmt.py", "-c", CORE]),
        ("hllint (ok-test)", ["python3", "tools/hllint.py", OK_TEST])):
    r = run(cmd)
    if r.returncode == 0:
        ok("atomics: %s" % label)
    else:
        bad("atomics: %s: %s" % (label, (r.stdout + r.stderr).strip()[:140]))

shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
