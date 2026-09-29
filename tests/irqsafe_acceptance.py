#!/usr/bin/env python3
"""Stage 91 acceptance gate — verified interrupt-safety (no alloc in
IRQ context).

Run with `make irqsafe-acceptance` (or `python3 tests/irqsafe_acceptance.py`).

Seven sections:

  1. the proof rejects        — every fail program is refused by BOTH
                                compilers with the SAME words (modulo
                                the known prefix), and each names its
                                own construct
  2. the witness chain        — the transitive shape: handler -> helper
                                -> offender, the shortest path, and a
                                deterministic answer across front-ends
  3. the audit lines          — `boot.py --audit` and `hlc --audit` both
                                publish the per-handler proof lines
  4. the ok-test              — feat_stage91_irqsafe on the interpreter,
                                natively, and linked -nostdlib (the
                                freestanding image of the same source)
  5. the classification       — synthesized probes: every may-allocate
                                construct rejected in a handler; every
                                borrow-shaped scalar operation accepted
  6. the demo                 — irqsafe_demo identical on the
                                interpreter and natively (-Werror), the
                                proof lines in its --audit
  7. the tools                — hlfmt stable, hllint clean on the new
                                sources
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
TMP = tempfile.mkdtemp(prefix="_gate_irqsafe_", dir=os.path.join(ROOT, "tests"))

OK_TEST = "tests/ok/feat_stage91_irqsafe.hls"
DEMO = "examples/irqsafe_demo.hls"

# (file stem, needle in the diagnostic, what the probe pins)
FAIL_PROGRAMS = [
    ("fail_stage91_irq_alloc_list", "builds a list",
     "a list literal in the handler itself"),
    ("fail_stage91_irq_alloc_transitive", "concatenates strings",
     "a helper that concatenates strings"),
    ("fail_stage91_irq_alloc_builtin", "calls the builtin 'read_file'",
     "a helper that reads a file"),
    ("fail_stage91_irq_alloc_clone", "clones a heap value",
     "a clone() in the handler"),
    ("fail_stage91_irq_alloc_extern", "a C call this proof cannot see into",
     "an extern call (opaque to the proof)"),
    ("fail_stage91_irq_alloc_mapnew", "calls the builtin 'map_new'",
     "a map construction in the handler"),
    ("fail_stage91_irq_alloc_default", "the field default of struct 'Log'",
     "a struct field default that builds a list"),
    ("fail_stage91_irq_tostr", "converts a value to its text form",
     "str(int) in the handler"),
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
    as the Stage 86 gate)."""
    out = re.sub(r"^panic: ", "", text.strip())
    out = re.sub(r"^[a-z ]*error: ", "", out)
    out = re.sub(r" \(line \d+(?::\d+)?\)\s*$", "", out)
    return out


def boot_check(src):
    r = run(["python3", "boot/boot.py", "--check", src])
    text = (r.stdout + r.stderr).strip()
    return r.returncode, text


def hlc_audit(src):
    r = run(["./bin/hlc", "--audit", src])
    text = (r.stdout + r.stderr).strip()
    return r.returncode, text


def native_run(src, extra_cflags=()):
    c = os.path.join(TMP, os.path.basename(src) + ".c")
    exe = os.path.join(TMP, os.path.basename(src) + ".bin")
    r = run(["./bin/hlc", src, c])
    if r.returncode != 0:
        return None, "hlc failed: " + (r.stdout + r.stderr)
    g = run(["gcc", "-O2", "-Werror"] + list(extra_cflags)
            + ["-o", exe, c, "-lm", "-pthread"])
    if g.returncode != 0:
        return None, "gcc failed: " + g.stderr
    rr = run([exe], timeout=60)
    return rr.returncode, rr.stdout


# ---------------------------------------------------------------------------
print("=== 1. the proof rejects: the fail programs, both front-ends ===")
# ---------------------------------------------------------------------------
for stem, needle, why in FAIL_PROGRAMS:
    src = "tests/fail/%s.hls" % stem
    brc, btxt = boot_check(src)
    hrc, htxt = hlc_audit(src)
    if brc == 0:
        bad("%s: boot accepted a handler that may allocate (%s)" % (stem, why))
        continue
    if hrc == 0:
        bad("%s: hlc accepted a handler that may allocate (%s)" % (stem, why))
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

# ---------------------------------------------------------------------------
print("=== 2. the witness chain: transitive, shortest, deterministic ===")
# ---------------------------------------------------------------------------
WITNESS = """struct Frame { p: int }

#[irq_handler(63)]
fn deep_isr(frame: Frame) -> void {
    middle(frame.p)
    return
}

fn middle(n: int) -> int {
    return builder(n)
}

fn builder(n: int) -> int {
    let s: str = "x" + "y"
    return n
}

fn main() -> int {
    return 0
}
"""
wpath = os.path.join(TMP, "witness.hls")
with open(wpath, "w") as fh:
    fh.write(WITNESS)
brc, btxt = boot_check(wpath)
hrc, htxt = hlc_audit(wpath)
if brc != 1 or hrc == 0:
    bad("witness: the three-deep chain was not rejected (boot rc=%d, hlc rc=%d)"
        % (brc, hrc))
else:
    bmsg = normalise(btxt.splitlines()[0])
    hmsg = normalise(htxt.splitlines()[0])
    if bmsg != hmsg:
        bad("witness: front-ends disagree: %s vs %s" % (bmsg, hmsg))
    elif "deep_isr -> middle -> builder" not in bmsg:
        bad("witness: the chain is not the full path: %s" % bmsg)
    elif "concatenates strings" not in bmsg:
        bad("witness: the offending node is not named: %s" % bmsg)
    else:
        ok("witness: the full transitive chain is named identically by both")

# The SHORTEST path wins: with two routes to the same offender, the
# diagnostic names the direct one.
SHORT = """struct Frame { p: int }

#[irq_handler(64)]
fn short_isr(frame: Frame) -> void {
    let s: str = "a" + "b"
    return
}

fn main() -> int {
    return 0
}
"""
spath = os.path.join(TMP, "short.hls")
with open(spath, "w") as fh:
    fh.write(SHORT)
brc, btxt = boot_check(spath)
hrc, htxt = hlc_audit(spath)
if brc != 1:
    bad("short: the direct site was not rejected")
elif "short_isr — 'short_isr' concatenates strings" not in normalise(btxt.splitlines()[0]):
    bad("short: the direct site is not reported as the handler itself: %s"
        % btxt)
elif normalise(btxt.splitlines()[0]) != normalise(htxt.splitlines()[0]):
    bad("short: front-ends disagree")
else:
    ok("short: a direct site is reported as the handler itself, both front-ends")

# ---------------------------------------------------------------------------
print("=== 3. the audit lines ===")
# ---------------------------------------------------------------------------
brc, _ = run(["python3", "boot/boot.py", "--audit", DEMO]), None
b_audit = run(["python3", "boot/boot.py", "--audit", DEMO])
h_audit = run(["./bin/hlc", "--audit", DEMO])
b_out = b_audit.stdout
h_out = h_audit.stdout
if b_audit.returncode != 0 or h_audit.returncode != 0:
    bad("audit: --audit failed (boot rc=%d, hlc rc=%d)"
        % (b_audit.returncode, h_audit.returncode))
elif "Interrupt safety (Stage 91)" not in b_out:
    bad("audit: boot --audit does not publish the proof header")
elif "IRQ vector 32: 'pit_isr' — allocation-free" not in b_out:
    bad("audit: boot --audit misses the pit_isr proof line")
elif "IRQ vector 44: 'disk_isr' — allocation-free" not in b_out:
    bad("audit: boot --audit misses the disk_isr proof line")
elif "Interrupt safety (Stage 91)" not in h_out:
    bad("audit: hlc --audit does not publish the proof header")
elif "IRQ vector 32: 'pit_isr' — allocation-free" not in h_out:
    bad("audit: hlc --audit misses the pit_isr proof line")
else:
    b_irq = [l for l in b_out.splitlines() if "allocation-free" in l]
    h_irq = [l for l in h_out.splitlines() if "allocation-free" in l]
    if b_irq != h_irq:
        bad("audit: the two front-ends publish different proof lines:\n    boot: %s\n    hlc:  %s"
            % (b_irq, h_irq))
    else:
        ok("audit: both front-ends publish the same per-handler proof lines")

# ---------------------------------------------------------------------------
print("=== 4. the ok-test on all three paths ===")
# ---------------------------------------------------------------------------
interp = run(["python3", "boot/boot.py", OK_TEST], timeout=300)
if interp.returncode == 0:
    ok("irqsafe: feat_stage91_irqsafe runs on the interpreter (exit 0)")
else:
    bad("irqsafe: feat_stage91_irqsafe interpreter rc=%d: %s"
        % (interp.returncode, (interp.stdout + interp.stderr)[:200]))

rc, out = native_run(OK_TEST)
if rc == 0:
    ok("irqsafe: feat_stage91_irqsafe compiles -Werror and runs natively (exit 0)")
else:
    bad("irqsafe: feat_stage91_irqsafe native rc=%s: %s" % (rc, (out or "")[:200]))

# The freestanding image of the same source: #![no_std] -> #![freestanding].
# Anchored to the whole line: the header COMMENT also contains the
# token ("#![no_std] so the same source runs..."), and a naive first-
# occurrence replace rewrites the comment, leaves the real attribute
# alone, and the -nostdlib link then segfaults on a hosted image.
fs_src = os.path.join(TMP, "irqsafe_fs.hls")
with open(OK_TEST) as fh:
    src = fh.read()
fs_text = re.sub(r"(?m)^#!\[no_std\]$", "#![freestanding]", src, count=1)
if fs_text == src:
    bad("irqsafe: the ok-test has no #![no_std] line to rewrite")
with open(fs_src, "w") as fh:
    fh.write(fs_text)
fs_c = os.path.join(TMP, "irqsafe_fs.c")
r = run(["./bin/hlc", fs_src, fs_c])
if r.returncode != 0:
    bad("irqsafe: freestanding emission failed: %s" % (r.stdout + r.stderr)[:200])
elif "HL_FREESTANDING" not in open(fs_c).read():
    bad("irqsafe: the freestanding rewrite produced a hosted TU")
else:
    g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
             "-fno-stack-protector", "-Wl,--gc-sections",
             "-o", os.path.join(TMP, "irqsafe_fs"), fs_c])
    if g.returncode != 0:
        bad("irqsafe: freestanding link failed: %s" % g.stderr[:300])
    else:
        rr = run([os.path.join(TMP, "irqsafe_fs")], timeout=60)
        if rr.returncode == 0:
            ok("irqsafe: the same source links -nostdlib and exits 0")
        else:
            bad("irqsafe: freestanding binary rc=%d" % rr.returncode)

# ---------------------------------------------------------------------------
print("=== 5. the classification: probes on both sides ===")
# ---------------------------------------------------------------------------
# (a) may-allocate constructs — each must be rejected in a handler.
MAY_ALLOC = [
    ("listlit", "let xs: list[int] = [1]", "builds a list"),
    ("mapnew", "let m: map[str, int] = map_new()",
     "calls the builtin 'map_new'"),
    ("concat", 'let s: str = "a" + "b"', "concatenates strings"),
    ("tostr", "let s: str = str(frame.p)", "converts a value to its text form"),
    ("inttostr", "let s: str = frame.p.to_str()", "converts a value to its text form"),
    ("clone", 'let name: str = clone("q")', "clones a heap value"),
    ("push", "q.push(2)", "calls 'list.push'"),
    ("slice", 'let s: str = "abc".slice(0, 1)', "calls 'str.slice'"),
    ("range", "let xs: list[int] = range(0, 4)", "calls the builtin 'range'"),
]
for stem, body, needle in MAY_ALLOC:
    if stem == "push":
        # The queue arrives THROUGH the frame (a list is pointer-typed,
        # exactly like the Stage 86 frame shapes) so 'push' is the
        # handler's only construct — a list literal would win the
        # first-site rule and the witness would name the wrong thing.
        prog = """#[irq_handler(90)]
fn probe_isr(q: list[int]) -> void {
    %s
    return
}

fn main() -> int {
    return 0
}
""" % body
    else:
        prog = """struct Frame { p: int }

#[irq_handler(90)]
fn probe_isr(frame: Frame) -> void {
    %s
    return
}

fn main() -> int {
    return 0
}
""" % body
    ppath = os.path.join(TMP, "alloc_%s.hls" % stem)
    with open(ppath, "w") as fh:
        fh.write(prog)
    brc, btxt = boot_check(ppath)
    hrc, htxt = hlc_audit(ppath)
    bmsg = normalise(btxt.splitlines()[0]) if brc else ""
    hmsg = normalise(htxt.splitlines()[0]) if hrc else ""
    if brc != 1 or hrc == 0:
        bad("probe %s: accepted (boot rc=%d, hlc rc=%d)" % (stem, brc, hrc))
    elif needle not in bmsg or needle not in hmsg:
        bad("probe %s: wrong words (boot: %s | hlc: %s)" % (stem, bmsg, hmsg))
    elif bmsg != hmsg:
        bad("probe %s: front-ends disagree" % stem)
    else:
        ok("probe %s rejected: %s" % (stem, needle))

# (b) the borrow-shaped world — everything here is accepted, because a
# handler that only reads, indexes, compares and counts is legal.
SAFE_PROBE = """struct Frame { p: int }

fn clamp(n: int, lo: int, hi: int) -> int {
    if n < lo {
        return lo
    }
    if n > hi {
        return hi
    }
    return n
}

#[irq_handler(91)]
fn safe_isr(frame: Frame) -> void {
    let n: int = clamp(frame.p, 0, 100)
    let mut i: int = 0
    while i < n {
        i = i + 2
    }
    if "abc".contains("b") != true {
        return
    }
    let code: int = "42".to_int()
    if code != 42 {
        return
    }
    return
}

fn main() -> int {
    return 0
}
"""
sppath = os.path.join(TMP, "safe_probe.hls")
with open(sppath, "w") as fh:
    fh.write(SAFE_PROBE)
brc, btxt = boot_check(sppath)
hrc, htxt = hlc_audit(sppath)
if brc != 0 or hrc != 0:
    bad("safe probe: rejected (boot rc=%d: %s | hlc rc=%d)"
        % (brc, btxt[:160], hrc))
else:
    ok("safe probe accepted: reads, compares, to_int, a pure helper")

# ---------------------------------------------------------------------------
print("=== 6. the demo ===")
# ---------------------------------------------------------------------------
d_interp = run(["python3", "boot/boot.py", DEMO], timeout=300)
rc, out = native_run(DEMO)
if d_interp.returncode == 0 and rc == 0 and out == d_interp.stdout:
    ok("irqsafe_demo: identical output on the interpreter and natively")
else:
    bad("irqsafe_demo: interpreter rc=%d, native rc=%s (outputs differ or failed)"
        % (d_interp.returncode, rc))

_, htxt = hlc_audit(DEMO)
if "IRQ vector 32: 'pit_isr' — allocation-free (proved over" in htxt:
    ok("irqsafe_demo: hlc --audit carries the proof lines")
else:
    bad("irqsafe_demo: proof lines missing from hlc --audit")

# ---------------------------------------------------------------------------
print("=== 7. the tools ===")
# ---------------------------------------------------------------------------
fmt1 = run(["python3", "tools/hlfmt.py", DEMO])
fmt2 = run(["python3", "tools/hlfmt.py", DEMO])
if fmt1.returncode == 0 and fmt2.returncode == 0 and fmt1.stdout == fmt2.stdout:
    ok("hlfmt: stable formatting on the demo")
else:
    bad("hlfmt: unstable or failed on the demo")

lint = run(["python3", "tools/hllint.py", DEMO])
lint2 = run(["python3", "tools/hllint.py", OK_TEST])
if lint.returncode == 0 and lint2.returncode == 0:
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings (demo rc=%d, ok rc=%d)" % (lint.returncode, lint2.returncode))

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 91 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
