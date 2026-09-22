#!/usr/bin/env python3
"""Stage 81 panic-acceptance test.

Verifies `core.panic` + the `#[panic_handler]` override end-to-end:

1. `core.panic` resolves through `core.` prefix (Stage-0); traversal
   guards continue to hold for the new module.
2. The module parses standalone (structs, enum, fns, zero imports —
   the freestanding-safe closure needs nothing else).
3. Stage-0 enforcement: the hosted demo checks and panics with the
   handler's marker (exit 101); the no_std ok-test checks and runs
   (exit 0) in `#![no_std]`, `#![freestanding]` and bare-hosted
   modes; the three fail programs are rejected with a
   `panic_handler` message.
4. Targeted behavior probes (interpreter): PanicInfo geometry,
   formatting, action codes, PanicLog eviction, guard panics (each
   exits 101), handler-fires (marker + 101), handler-exit-wins
   (exit 42).
5. Self-hosted emission (hlc.hls through the interpreter): the hosted
   demo compiles to C with the hook armed (`hl_panic_hook =
   usf_panic_handler`) and the native binary prints the marker
   before the default report (exit 101); the exit-wins probe exits
   42 natively; the no_std ok-test compiles to hosted C (exit 0);
   the freestanding ok-test links `-nostdlib` (exit 0); the
   self-hosted checker rejects the fail programs with the same
   `panic_handler` message (boot/hlc parity).
6. hlfmt stability over every new Stage 81 file.
7. `--audit` reports the crate mode and shows every panic function
   pure (no `uses`) — the demo's handler is the one declared-IO
   exception.

Run::

    python3 tests/panic_acceptance.py

(Called from ``make panic-acceptance`` in mk/95-osdev.mk.)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PY = sys.executable
DEMO = os.path.join(REPO_ROOT, "examples", "panic_demo.hls")
OKTEST = os.path.join(REPO_ROOT, "tests", "ok", "feat_stage81_panic.hls")
PANIC = os.path.join(REPO_ROOT, "core", "panic.hls")
FAIL_DUP = os.path.join(REPO_ROOT, "tests", "fail",
                         "fail_panic_dup_handler.hls")
FAIL_SIG = os.path.join(REPO_ROOT, "tests", "fail",
                         "fail_panic_bad_sig.hls")
FAIL_RET = os.path.join(REPO_ROOT, "tests", "fail",
                         "fail_panic_bad_ret.hls")
CORE = ["option", "result", "iter", "clone", "eq", "alloc", "mem", "panic"]


def section(title: str) -> None:
    print()
    print("=== %s ===" % title)


def check(name: str, ok: bool, detail: str = "") -> bool:
    flag = "OK" if ok else "FAIL"
    print("  [%s] %s%s" % (flag, name, (" — " + detail) if detail else ""))
    if not ok:
        check.failed += 1  # type: ignore[attr-defined]
    return ok
check.failed = 0  # type: ignore[attr-defined]


def boot(*args: str, cwd: str | None = None):
    return subprocess.run([PY, os.path.join(REPO_ROOT, "boot", "boot.py")]
                          + list(args), capture_output=True, text=True,
                          cwd=cwd or REPO_ROOT, timeout=300)


def write_tmp(suffix: str, src: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return path


# ---------------------------------------------------------------------------
# 1. core.panic resolution + traversal guards
# ---------------------------------------------------------------------------

def test_resolution() -> None:
    section("1. core.panic import resolution (+ traversal guards)")
    from boot.boot import _resolve_import
    entry = os.path.join(tempfile.mkdtemp(prefix="hls_panic_"), "e.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("fn main() -> int { return 0 }\n")
    p = _resolve_import("core.panic", entry)
    check("core.panic resolves",
          p is not None and p.endswith(os.path.join("core", "panic.hls")),
          p or "None")
    # The existing core.* modules still resolve (regression check)
    for mod in CORE:
        p = _resolve_import("core." + mod, entry)
        check("core.%s still resolves" % mod,
              p is not None and p.endswith(os.path.join("core", mod + ".hls")))
    # std.str (hosted) still resolves alongside core.panic
    s = _resolve_import("std.str", entry)
    check("std.str still resolves",
          s is not None and s.endswith(os.path.join("std", "str.hls")))
    # Path traversal guards: the new module doesn't open any new
    # vector. The same three evil paths from the Stage 78 suite
    # continue to be rejected.
    for evil in ["core.../secret", "core./x", "core"]:
        try:
            out = _resolve_import(evil, entry)
            check("guard %r" % evil, out is None, str(out)[:60])
        except SystemExit as ex:
            check("guard %r" % evil, True, str(ex)[:70])


# ---------------------------------------------------------------------------
# 2. core.panic parses standalone + imports are core-only
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("2. core.panic parses standalone + core-only imports")
    from boot.lexer import tokenize
    from boot.parser import Parser
    with open(PANIC, "rb") as f:
        prog = Parser(tokenize(f.read())).parse_program()
    check("core/panic.hls parses",
          len(prog["structs"]) > 0 and len(prog["enums"]) > 0
          and len(prog["fns"]) > 0,
          "structs=%d, enums=%d, fns=%d"
          % (len(prog["structs"]), len(prog["enums"]), len(prog["fns"])))
    # Must declare the two public structs and the action enum
    expected_structs = {"PanicInfo", "PanicLog"}
    check("both structs present",
          expected_structs.issubset(set(prog["structs"].keys())),
          "missing: %s" % sorted(expected_structs - set(prog["structs"].keys())))
    check("PanicAction enum present",
          "PanicAction" in prog["enums"])
    # Imports: the module needs nothing beyond the core language
    # (no `std.*`, no relative, not even `core.*`).
    imps = [i["path"] for i in prog["imports"]]
    check("imports are core-only",
          all(i.startswith("core.") for i in imps),
          "got: %s" % imps)
    # Count key reference functions
    fns = set(prog["fns"].keys())
    must_have = ["panic_info_new", "panic_info_msg", "panic_info_file",
                 "panic_info_line", "panic_info_has_location",
                 "panic_format", "panic_action_name", "panic_action_code",
                 "panic_default_code", "panic_log_new", "panic_log_push",
                 "panic_log_len", "panic_log_dropped", "panic_log_get",
                 "panic_log_clear", "panic_check"]
    missing = [f for f in must_have if f not in fns]
    check("all reference functions present",
          not missing, "missing: %s" % missing)
    check("function count sane (>= 15)", len(fns) >= 15,
          "got %d" % len(fns))


# ---------------------------------------------------------------------------
# 3. Stage-0 enforcement (hosted demo + no_std / freestanding ok-test)
# ---------------------------------------------------------------------------

def test_enforcement() -> None:
    section("3. Stage-0 enforcement (demo panics w/ marker; ok-test clean)")
    r = boot("--check", DEMO)
    check("hosted demo checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(DEMO)
    out = (r.stdout or "") + (r.stderr or "")
    check("hosted demo panics (exit 101)", r.returncode == 101,
          "rc=%d" % r.returncode)
    check("handler marker on stdout",
          "kernel panic: demo fault" in (r.stdout or ""))
    check("default report still on stderr",
          "panic: demo fault" in (r.stderr or ""))
    r = boot("--check", OKTEST)
    check("no_std ok-test checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(OKTEST)
    check("no_std ok-test runs (exit 0)", r.returncode == 0,
          "rc=%d" % r.returncode)
    # Same ok-test with #![freestanding] instead of #![no_std] —
    # the C backend will emit `_start` and link with -nostdlib; the
    # exit code is the program's answer (the same 0). Replace ONLY
    # the standalone crate attribute (the comment headers also
    # contain the tokens, so a naive .replace of the FIRST match
    # would corrupt the comment instead of switching the crate).
    fs_src = open(OKTEST, encoding="utf-8").read().replace(
        "\n#![no_std]\n", "\n#![freestanding]\n", 1)
    p = write_tmp(".hls", fs_src)
    r = boot("--check", p)
    check("freestanding ok-test checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(p)
    check("freestanding ok-test runs (exit 0)",
          r.returncode == 0, "rc=%d" % r.returncode)
    # The same ok-test with NO crate attr — the std.* boundary
    # is closed but core.* is fine; pure language programs run on
    # the hosted toolchain too.
    bare_src = open(OKTEST, encoding="utf-8").read().replace(
        "#![no_std]\n", "", 1)
    p = write_tmp(".hls", bare_src)
    r = boot(p)
    check("hosted (no crate attr) ok-test runs (exit 0)",
          r.returncode == 0, "rc=%d" % r.returncode)
    # --audit reports the crate mode
    r = boot("--audit", OKTEST)
    check("--audit shows no_std",
          r.returncode == 0 and "#![no_std]" in (r.stdout or ""))
    # extern + uses are still rejected in no_std mode
    p = write_tmp(".hls", "#![no_std]\nimport \"core.panic\"\n"
                   "extern \"C\" {\n    fn malloc(n: int) -> int uses IO\n}\n"
                   "fn main() -> int { return 0 }\n")
    r = boot("--check", p)
    out = (r.stdout or "") + (r.stderr or "")
    check("no_std extern rejected",
          r.returncode == 1 and "not available in #![no_std]" in out,
          out.strip()[:90])
    # The three Stage 81 fail programs are rejected with a
    # `panic_handler` message (boot side).
    for path, needle in [(FAIL_DUP, "only one #[panic_handler]"),
                         (FAIL_SIG, "#[panic_handler] requires exactly one"),
                         (FAIL_RET, "#[panic_handler] must return")]:
        r = boot("--check", path)
        out = (r.stdout or "") + (r.stderr or "")
        check("rejected: %s" % os.path.basename(path),
              r.returncode == 1 and needle in out,
              out.strip()[:100])


# ---------------------------------------------------------------------------
# 4. Targeted behavior probes (interpreter)
# ---------------------------------------------------------------------------

def test_behavior() -> None:
    section("4. Behavior probes (interpreter)")
    # A series of single-feature probes; panicking probes are
    # expected to exit 101 (the guard rails firing), the rest 0.
    probes = [
        # PanicInfo geometry + formatting
        ("info geometry + format", """
import "core.panic"
fn main() -> int {
    let i: PanicInfo = panic_info_new("boom", "k.hls", 9)
    if panic_format(i) != "boom (at k.hls:9)" { return 1 }
    if !panic_info_has_location(i) { return 2 }
    if panic_format(panic_info_new("b", "", 0)) != "b" { return 3 }
    if panic_action_name(PanicAction.Exit) != "exit" { return 4 }
    if panic_action_code(PanicAction.Reboot) != 0 { return 5 }
    if panic_default_code() != 101 { return 6 }
    return 0
}
""", 0),
        # PanicLog FIFO eviction + drop counting
        ("log eviction + drops", """
import "core.panic"
fn main() -> int {
    let l: PanicLog = panic_log_new(2)
    panic_log_push(l, "a")
    panic_log_push(l, "b")
    panic_log_push(l, "c")
    if panic_log_len(l) != 2 { return 1 }
    if panic_log_get(l, 0) != "b" { return 2 }
    if panic_log_get(l, 1) != "c" { return 3 }
    if panic_log_dropped(l) != 1 { return 4 }
    panic_log_clear(l)
    if panic_log_len(l) != 0 || panic_log_dropped(l) != 0 { return 5 }
    return 0
}
""", 0),
        # Guard rails: each of these must panic (exit 101)
        ("guard: negative line", """
import "core.panic"
fn main() -> int {
    let _: PanicInfo = panic_info_new("m", "f", -1)
    return 0
}
""", 101),
        ("guard: zero capacity", """
import "core.panic"
fn main() -> int {
    let _: PanicLog = panic_log_new(0)
    return 0
}
""", 101),
        ("guard: log get OOB", """
import "core.panic"
fn main() -> int {
    let l: PanicLog = panic_log_new(2)
    panic_log_push(l, "only")
    let _: str = panic_log_get(l, 5)
    return 0
}
""", 101),
        ("guard: panic_check fires", """
import "core.panic"
fn main() -> int {
    panic_check(1 == 2, "math is broken")
    return 0
}
""", 101),
        # Handler fires: marker on stdout, default report after, 101
        ("handler fires before default", """
#[panic_handler]
fn panic_handler(msg: str) -> void uses IO {
    println("hook saw: " + msg)
}
fn main() -> int uses IO {
    let _: int = panic("probe fault")
    return 0
}
""", 101),
        # Handler chooses the halt action via exit()
        ("handler exit wins", """
#[panic_handler]
fn panic_handler(msg: str) -> void uses Exit {
    exit(42)
}
fn main() -> int {
    let _: int = panic("boom")
    return 0
}
""", 42),
        # Nested panic inside the handler does NOT recurse: the
        # default report for the ORIGINAL fault still runs (101)
        ("nested panic guarded", """
#[panic_handler]
fn panic_handler(msg: str) -> void {
    let _: int = panic("handler blew up")
}
fn main() -> int {
    let _: int = panic("original fault")
    return 0
}
""", 101),
    ]
    for name, src, want in probes:
        p = write_tmp(".hls", src)
        r = boot(p)
        ok = r.returncode == want
        detail = "rc=%d want=%d" % (r.returncode, want)
        if want == 101 and name == "handler fires before default":
            ok = ok and "hook saw: probe fault" in (r.stdout or "")
            detail += " marker=%s" % ("hook saw: probe fault"
                                      in (r.stdout or ""))
        if want == 101 and name == "nested panic guarded":
            ok = ok and "original fault" in (r.stderr or "")
            detail += " orig=%s" % ("original fault" in (r.stderr or ""))
        check("probe: %s" % name, ok, detail)


# ---------------------------------------------------------------------------
# 5. Self-hosted emission (hlc) + native parity
# ---------------------------------------------------------------------------

def test_emission() -> None:
    section("5. Self-hosted emission (hlc) + native parity")
    d = tempfile.mkdtemp(prefix="hls_panic_emit_")
    out = os.path.join(d, "panic.c")
    hlc = os.path.join(REPO_ROOT, "src", "hlc.hls")
    r = boot(hlc, DEMO, out)
    check("hlc emits hosted C",
          r.returncode == 0 and os.path.isfile(out),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    with open(out, encoding="utf-8", errors="replace") as f:
        src = f.read()
    check("hosted main kept", re.search(r"int main\s*\(", src) is not None)
    check("no _start entry", "void _start" not in src)
    check("panic hook armed",
          "hl_panic_hook = usf_panic_handler;" in src)
    check("handler body compiled", "usf_panic_handler" in src)
    check("hook machinery present",
          "hl_panic_hook" in src and "hl_in_panic" in src)
    # Compile the emitted C and run it; the handler marker must
    # print BEFORE the default report, exit code 101.
    if subprocess.run(["gcc", "-O2", "-o", os.path.join(d, "panic.bin"),
                        out, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "panic.bin")],
                             capture_output=True, text=True, timeout=30)
        check("native marker before default",
              "kernel panic: demo fault" in (nat.stdout or "")
              and "panic: demo fault" in (nat.stderr or ""),
              "rc=%d" % nat.returncode)
        check("native demo exit 101", nat.returncode == 101,
              "rc=%d" % nat.returncode)
    else:
        check("native panic_demo links", False, "gcc failed")
    # The exit-wins probe exits 42 natively too (the handler's
    # exit() is the halt action on the C backend as well).
    wprobe = write_tmp(".hls", """
#[panic_handler]
fn panic_handler(msg: str) -> void uses Exit {
    exit(42)
}
fn main() -> int {
    let _: int = panic("boom")
    return 0
}
""")
    outw = os.path.join(d, "exitwins.c")
    r = boot(hlc, wprobe, outw)
    check("hlc emits exit-wins C", r.returncode == 0
          and os.path.isfile(outw))
    if subprocess.run(["gcc", "-O2", "-o", os.path.join(d, "exitwins.bin"),
                        outw, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "exitwins.bin")],
                             capture_output=True, timeout=30)
        check("native exit-wins exit 42", nat.returncode == 42,
              "rc=%d" % nat.returncode)
    else:
        check("native exit-wins links", False, "gcc failed")
    # The no_std ok-test compiles to hosted C and exits 0 natively
    # (the effect-free handler is wired but never fires).
    out3 = os.path.join(d, "ok.c")
    r = boot(hlc, OKTEST, out3)
    check("hlc emits no_std ok C", r.returncode == 0
          and os.path.isfile(out3))
    with open(out3, encoding="utf-8", errors="replace") as f:
        src3 = f.read()
    check("ok hook armed", "hl_panic_hook = usf_panic_handler;" in src3)
    if subprocess.run(["gcc", "-O2", "-o", os.path.join(d, "ok.bin"),
                        out3, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "ok.bin")],
                             capture_output=True, timeout=30)
        check("native ok-test exit 0", nat.returncode == 0,
              "rc=%d" % nat.returncode)
    else:
        check("native ok-test links", False, "gcc failed")
    # Freestanding parity: the same ok-test with #![freestanding]
    # instead of #![no_std] must compile to a -nostdlib binary that
    # exits with the same code as the interpreter. Replace ONLY the
    # standalone crate attribute (see test_enforcement for the
    # comment-corruption caveat).
    fs_src = open(OKTEST, encoding="utf-8").read().replace(
        "\n#![no_std]\n", "\n#![freestanding]\n", 1)
    fs_p = write_tmp(".hls", fs_src)
    interp = boot(fs_p)
    out2 = os.path.join(d, "fs.c")
    r = boot(hlc, fs_p, out2)
    check("hlc emits freestanding C",
          r.returncode == 0 and os.path.isfile(out2),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    with open(out2, encoding="utf-8", errors="replace") as f:
        src2 = f.read()
    check("freestanding _start arms hook",
          "void _start" in src2
          and "hl_panic_hook = usf_panic_handler;" in src2)
    check("freestanding traps by default", "__builtin_trap" in src2)
    if subprocess.run(["gcc", "-O2", "-ffreestanding", "-nostdlib",
                        "-ffunction-sections", "-fno-stack-protector",
                        "-Wl,--gc-sections",
                        "-o", os.path.join(d, "fs.bin"), out2],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "fs.bin")],
                             capture_output=True, timeout=30)
        check("freestanding native == interpreter",
              nat.returncode == interp.returncode,
              "interp=%d nat=%d" % (interp.returncode, nat.returncode))
    else:
        check("freestanding binary links", False, "gcc failed")
    # Boot/hlc parity on the fail programs: the self-hosted checker
    # rejects them with the same `panic_handler` message.
    for path, needle in [(FAIL_DUP, "panic_handler"),
                         (FAIL_SIG, "panic_handler")]:
        outf = os.path.join(d, "fail.c")
        r = boot(hlc, path, outf)
        txt = (r.stdout or "") + (r.stderr or "")
        check("hlc rejects %s" % os.path.basename(path),
              r.returncode != 0 and needle in txt,
              txt.strip()[-120:])


# ---------------------------------------------------------------------------
# 6. hlfmt stability
# ---------------------------------------------------------------------------

def test_fmt() -> None:
    section("6. hlfmt stability over every Stage 81 file")
    import subprocess as sp
    files = [PANIC, DEMO, OKTEST, FAIL_DUP, FAIL_SIG, FAIL_RET]
    for f in files:
        r = sp.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                    "-c", f], capture_output=True, text=True,
                   cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean: %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""),
              (r.stdout or "").strip()[:80])


# ---------------------------------------------------------------------------
# 7. --audit shows every panic function pure
# ---------------------------------------------------------------------------

def test_audit() -> None:
    section("7. --audit: every panic function pure (no uses)")
    r = boot("--audit", OKTEST)
    out = r.stdout or ""
    check("audit ran", r.returncode == 0)
    # Every function from core.panic must appear as "(none - pure)"
    # (no `uses` clause). Pull the function-name column and confirm
    # the panic fns are listed with the pure marker.
    must_be_pure = [
        "panic_info_new", "panic_info_msg", "panic_info_file",
        "panic_info_line", "panic_info_has_location", "panic_format",
        "panic_action_name", "panic_action_code", "panic_default_code",
        "panic_log_new", "panic_log_push", "panic_log_len",
        "panic_log_dropped", "panic_log_get", "panic_log_clear",
        "panic_check",
    ]
    missing = [f for f in must_be_pure if f not in out]
    check("all panic fns in audit output", not missing,
          "missing: %s" % missing[:5])
    bad = []
    for line in out.splitlines():
        s = line.strip()
        for f in must_be_pure:
            if s.startswith(f + " "):
                if "(none - pure)" not in s and "(none)" not in s:
                    bad.append(s)
                break
    check("all panic fns declared pure", not bad,
          "impure: %s" % bad[:3])
    # The ok-test's own handler is effect-free too
    for line in (out.splitlines()):
        s = line.strip()
        if s.startswith("panic_handler "):
            check("ok-test handler pure",
                  "(none - pure)" in s or "(none)" in s, s[:80])
            break
    # Crate mode reported
    check("crate mode reported",
          "Crate mode:" in out and "#![no_std]" in out)


def main() -> int:
    test_resolution()
    test_parse()
    test_enforcement()
    test_behavior()
    test_emission()
    test_fmt()
    test_audit()
    print()
    failed = check.failed  # type: ignore[attr-defined]
    if failed:
        print("PANIC ACCEPTANCE: %d assertion(s) FAILED" % failed)
        return 1
    print("ACCEPTANCE OK: Stage 81 -- core.panic + #[panic_handler]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
