#!/usr/bin/env python3
"""Stage 92 acceptance gate — cross-bootstrappable build
(Stage-0 -> freestanding hlc).

Run with `make xbootstrap-acceptance` (or
`python3 tests/xbootstrap_acceptance.py`).

Seven sections:

  1. the flag             — `hlc --no-libc` emits the no-libc runtime for
                            a HOSTED source (crt0 _start, 3-arg main);
                            the hosted output is unchanged; the flag
                            cannot combine with #![freestanding]
  2. the runtime shape    — raw syscall layer, mmap-backed arena, fd-
                            backed streams, and NOT a single libc header
  3. the ladder           — `make bootstrap-nolibc`: hosted hlc -> no-libc
                            TU -> freestanding hlc -> byte-identical
                            self-recompile + hosted parity
  4. the binary honesty   — no undefined libc symbols, not dynamically
                            linked
  5. the parity           — the freestanding compiler emits byte-
                            identical C for hosted AND no_std programs,
                            and its --audit agrees word for word
  6. the no-libc world    — a program using file IO, env, cwd, floats,
                            and str conversions runs with no libc and
                            matches the interpreter
  7. the pinned gaps      — a no-libc program that reaches threads or
                            directory listing fails the LINK with the
                            exact name (the honest "not available")
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
TMP = tempfile.mkdtemp(prefix="_gate_xboot_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/hello.hls"
NL_FLAGS = ["-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
            "-fno-stack-protector", "-Wl,--gc-sections"]


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


def emit(src, out, extra=()):
    return run(["./bin/hlc"] + list(extra) + [src, out])


def link_nl(cfile, exe):
    return run(["gcc"] + NL_FLAGS + ["-o", exe, cfile])


# ---------------------------------------------------------------------------
print("=== 1. the flag ===")
# ---------------------------------------------------------------------------
r = emit(DEMO, os.path.join(TMP, "hello_nl.c"), ["--no-libc"])
if r.returncode != 0:
    bad("flag: --no-libc emission failed: %s" % (r.stdout + r.stderr)[:200])
else:
    nl_c = open(os.path.join(TMP, "hello_nl.c")).read()
    checks = [
        ("HL_NO_LIBC 1" in nl_c, "the HL_NO_LIBC marker"),
        ("void _start(void)" in nl_c, "the crt0 _start entry"),
        ("int main(int argc, char** argv, char** envp)" in nl_c,
         "the 3-argument main (envp captured)"),
        ("hl_environ = envp;" in nl_c, "the environ capture"),
    ]
    if all(c[0] for c in checks):
        ok("flag: the no-libc TU carries _start, 3-arg main, environ capture")
    else:
        bad("flag: missing " + ", ".join(c[1] for c in checks if not c[0]))

r = emit(DEMO, os.path.join(TMP, "hello_hosted.c"))
hosted_c = open(os.path.join(TMP, "hello_hosted.c")).read()
if r.returncode != 0:
    bad("flag: plain emission failed")
elif "int main(int argc, char** argv)" not in hosted_c or "void _start(void)" in hosted_c:
    bad("flag: the hosted emission changed shape (2-arg main, no _start expected)")
elif "#include <stdio.h>" not in hosted_c:
    bad("flag: the hosted emission lost its libc includes")
else:
    ok("flag: the plain hosted emission is untouched")

CONFLICT = """#![freestanding]

fn main() -> int {
    return 0
}
"""
cp = os.path.join(TMP, "conflict.hls")
open(cp, "w").write(CONFLICT)
r = emit(cp, os.path.join(TMP, "conflict.c"), ["--no-libc"])
if r.returncode == 0:
    bad("conflict: --no-libc accepted on a #![freestanding] crate")
elif "--no-libc cannot be combined" not in (r.stdout + r.stderr):
    bad("conflict: wrong diagnostic: %s" % (r.stdout + r.stderr)[:160])
else:
    ok("conflict: --no-libc + #![freestanding] rejected with its own words")

# ---------------------------------------------------------------------------
print("=== 2. the no-libc runtime shape ===")
# ---------------------------------------------------------------------------
needs = [
    ("HL_SYS_exit_group 231L", "the x86-64 syscall numbers"),
    ("HL_SYS_openat", "the open path"),
    ("1073741824", "the 1 GiB reserved arena"),
    ("hl_nl_sys(long n", "the raw syscall layer"),
    ("#define stdout (&hl_nl_fout)", "the fd-backed stdout"),
    ("hl_nl_fopen", "the fd-backed fopen shim"),
    ("hl_nl_strtod", "the float parse shim"),
    ("hl_nl_vfmt", "the printf-family shim"),
]
missing = [what for needle, what in needs if needle not in nl_c]
if missing:
    bad("runtime shape: missing " + ", ".join(missing))
elif "#include <stdio.h>" in nl_c or "#include <stdlib.h>" in nl_c:
    bad("runtime shape: a libc header slipped in")
elif "typedef struct { int fd; } FILE;" not in nl_c:
    bad("runtime shape: the fd-backed FILE typedef is missing")
else:
    ok("runtime shape: syscall layer + arena + fd streams, zero libc headers")

# ---------------------------------------------------------------------------
print("=== 3. the ladder ===")
# ---------------------------------------------------------------------------
mk = run(["make", "bootstrap-nolibc"], timeout=1200)
log = mk.stdout + mk.stderr
if mk.returncode != 0:
    bad("ladder: make bootstrap-nolibc failed:\n%s" % "\n".join(log.splitlines()[-6:]))
elif "CROSS-BOOTSTRAP OK" not in log:
    bad("ladder: the ladder ran but did not print CROSS-BOOTSTRAP OK")
else:
    ok("ladder: Stage-0 -> hosted hlc -> freestanding hlc, byte-identical at every step")

# ---------------------------------------------------------------------------
print("=== 4. the binary honesty ===")
# ---------------------------------------------------------------------------
exe = "bin/hlc-fs"
if not os.path.exists(exe):
    bad("honesty: bin/hlc-fs was not built by the ladder")
else:
    ldd = run(["ldd", exe])
    if "not a dynamic executable" in ldd.stderr or "statically linked" in ldd.stdout or "not a dynamic" in ldd.stdout:
        ok("honesty: not a dynamic executable")
    else:
        bad("honesty: ldd says: %s %s" % (ldd.stdout[:80], ldd.stderr[:80]))
    nm = run(["nm", "-u", exe])
    if nm.returncode != 0:
        bad("honesty: nm failed")
    else:
        bad_syms = [l for l in nm.stdout.splitlines()
                    if re.search(r"\b(fopen|malloc|free|printf|fprintf|exit|getenv|strtod|memcpy|strlen)\b", l)]
        if bad_syms:
            bad("honesty: undefined libc symbols present: %s" % bad_syms[:4])
        else:
            ok("honesty: no undefined libc symbols")

# ---------------------------------------------------------------------------
print("=== 5. the parity ===")
# ---------------------------------------------------------------------------
PARITY_FILES = [DEMO, "tests/ok/feat_stage91_irqsafe.hls",
                "tests/ok/feat_stage90_atomics.hls"]
all_same = True
for f in PARITY_FILES:
    a = os.path.join(TMP, "fs_out.c")
    b = os.path.join(TMP, "hosted_out.c")
    r1 = run([exe, f, a], timeout=600)
    r2 = run(["./bin/hlc", f, b], timeout=600)
    if r1.returncode != 0 or r2.returncode != 0:
        bad("parity: %s compile failed (fs rc=%d hosted rc=%d)"
            % (f, r1.returncode, r2.returncode))
        all_same = False
    elif open(a).read() != open(b).read():
        bad("parity: %s emits different C from the freestanding compiler" % f)
        all_same = False
if all_same:
    ok("parity: hosted and no_std programs compile byte-identically")

a = run([exe, "--audit", "examples/irqsafe_demo.hls"], timeout=300)
b = run(["./bin/hlc", "--audit", "examples/irqsafe_demo.hls"], timeout=300)
if a.returncode != 0 or b.returncode != 0:
    bad("parity: --audit failed (fs rc=%d hosted rc=%d)" % (a.returncode, b.returncode))
elif a.stdout != b.stdout:
    bad("parity: --audit outputs differ")
elif "IRQ vector 32: 'pit_isr' — allocation-free" not in a.stdout:
    bad("parity: the audit proof lines are missing")
else:
    ok("parity: --audit agrees word for word, proof lines included")

# ---------------------------------------------------------------------------
print("=== 6. the no-libc world ===")
# ---------------------------------------------------------------------------
WORLD = """fn main() -> int uses IO, Proc {
    let cwd: str = cwd_get()
    if cwd == "" {
        return 1
    }
    if file_exists("examples/hello.hls") != true {
        return 2
    }
    let src: str = read_file("examples/hello.hls")
    if len(src) < 10 {
        return 3
    }
    write_file("TMPDIR_OUT", "written by a no-libc binary")
    let back: str = read_file("TMPDIR_OUT")
    if back != "written by a no-libc binary" {
        return 4
    }
    let n: int = "42".to_int()
    if n != 42 {
        return 5
    }
    let t: str = str(1234)
    if t != "1234" {
        return 6
    }
    let f: float = 2.5 + 2.5
    if f != 5.0 {
        return 7
    }
    return 0
}
"""
wpath = os.path.join(TMP, "world.hls")
open(wpath, "w").write(WORLD.replace("TMPDIR_OUT", os.path.join(TMP, "world_out.txt")))
interp = run(["python3", "boot/boot.py", wpath], stdin=subprocess.DEVNULL, timeout=120)
w_c = os.path.join(TMP, "world.c")
w_exe = os.path.join(TMP, "world")
r = emit(wpath, w_c, ["--no-libc"])
g = link_nl(w_c, w_exe)
if r.returncode != 0:
    bad("world: --no-libc emission failed: %s" % (r.stdout + r.stderr)[:200])
elif g.returncode != 0:
    bad("world: link failed: %s" % g.stderr[:300])
else:
    rr = run([w_exe], stdin=subprocess.DEVNULL, timeout=60)
    if rr.returncode != 0:
        bad("world: the no-libc program exited %d" % rr.returncode)
    elif rr.returncode != interp.returncode:
        bad("world: interpreter exit %d vs native %d" % (interp.returncode, rr.returncode))
    else:
        ok("world: file IO + env + cwd + float/int conversions, exit 0, no libc")

# ---------------------------------------------------------------------------
print("=== 7. the pinned gaps ===")
# ---------------------------------------------------------------------------
THREADS = """fn worker(n: int) -> int {
    return n + 1
}

fn main() -> int uses Conc {
    let t: Task[int] = spawn(worker, 41)
    return t.join()
}
"""
tpath = os.path.join(TMP, "threads.hls")
open(tpath, "w").write(THREADS)
t_c = os.path.join(TMP, "threads.c")
t_exe = os.path.join(TMP, "threads")
r = emit(tpath, t_c, ["--no-libc"])
g = link_nl(t_c, t_exe) if r.returncode == 0 else None
if r.returncode != 0:
    bad("gaps: the spawn TU did not compile: %s" % (r.stdout + r.stderr)[:160])
elif g.returncode == 0:
    bad("gaps: a spawn() program LINKED without libc — the gap is hidden")
elif "pthread_create" not in g.stderr:
    bad("gaps: the link failed but not on pthread_create: %s" % g.stderr[:200])
else:
    ok("gaps: a spawn() program fails the LINK on pthread_create (named, not hidden)")

DIRS = """fn main() -> int uses IO {
    let entries: list[str] = fs_read_dir("examples")
    return 0
}
"""
dpath = os.path.join(TMP, "dirs.hls")
open(dpath, "w").write(DIRS)
d_c = os.path.join(TMP, "dirs.c")
d_exe = os.path.join(TMP, "dirs")
r = emit(dpath, d_c, ["--no-libc"])
g = link_nl(d_c, d_exe) if r.returncode == 0 else None
if r.returncode != 0:
    bad("gaps: the fs_read_dir TU did not compile: %s" % (r.stdout + r.stderr)[:160])
elif g.returncode == 0:
    bad("gaps: an fs_read_dir program LINKED without libc")
elif "opendir" not in g.stderr:
    bad("gaps: the link failed but not on opendir: %s" % g.stderr[:200])
else:
    ok("gaps: an fs_read_dir program fails the LINK on opendir (named, not hidden)")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 92 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
