#!/usr/bin/env python3
"""Stage 82 stack-acceptance test.

Verifies the deterministic stack budget + guard pages end to end:

1. `core.stack` resolves through `core.` prefix (Stage-0); traversal
   guards continue to hold for the new module.
2. The module parses standalone (structs, enum, fns, zero imports —
   the freestanding-safe closure needs nothing else).
3. Stage-0 enforcement: the hosted demo plans, probes, hits the guard
   (exit 101 with the handler marker); the ok-test checks and runs
   (exit 0) in `#![no_std]`, `#![freestanding]` and bare-hosted modes;
   the three fail programs are rejected with the Stage 82 messages.
4. Targeted probes (interpreter): the crate budget accepts a fitting
   program and `--audit` reports the verified worst chain; the
   over-budget and recursion-cycle programs are rejected.
5. Self-hosted emission (hlc.hls through the interpreter): the demo
   compiles to C and the native binary prints the marker (exit 101);
   the ok-test compiles to hosted C (exit 0); the freestanding ok-test
   links `-nostdlib` (exit 0); the task runtime carries the
   deterministic-stack machinery (`hl_thread_start`,
   `HL_TASK_STACK_BYTES`, `pthread_attr_setguardsize`); the
   self-hosted checker rejects the fail programs with the same
   messages (boot/hlc parity).
6. hlfmt stability over every new Stage 82 file.
7. `--audit` shows every core.stack function pure (no `uses`).

Run::

    python3 tests/stack_acceptance.py

(Called from ``make stackguard-acceptance`` in mk/95-osdev.mk.)
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
DEMO = os.path.join(REPO_ROOT, "examples", "stack_guard_demo.hls")
OKTEST = os.path.join(REPO_ROOT, "tests", "ok", "feat_stage82_stack.hls")
STACK = os.path.join(REPO_ROOT, "core", "stack.hls")
FAIL_BUDGET = os.path.join(REPO_ROOT, "tests", "fail",
                           "fail_stack_budget_exceeded.hls")
FAIL_RECUR = os.path.join(REPO_ROOT, "tests", "fail",
                          "fail_stack_budget_recursion.hls")
FAIL_FNBODY = os.path.join(REPO_ROOT, "tests", "fail",
                           "fail_stack_size_fn.hls")
CORE = ["option", "result", "iter", "clone", "eq", "alloc", "mem",
        "panic", "stack"]
GCC = os.environ.get("CC", "gcc")


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
# 1. core.stack resolution + traversal guards
# ---------------------------------------------------------------------------

def test_resolution() -> None:
    section("1. core.stack import resolution (+ traversal guards)")
    from boot.boot import _resolve_import
    entry = os.path.join(tempfile.mkdtemp(prefix="hls_stack_"), "e.hls")
    with open(entry, "w", encoding="utf-8", newline="\n") as f:
        f.write("fn main() -> int { return 0 }\n")
    p = _resolve_import("core.stack", entry)
    check("core.stack resolves",
          p is not None and p.endswith(os.path.join("core", "stack.hls")),
          p or "None")
    # The existing core.* modules still resolve (regression check)
    for mod in CORE:
        p = _resolve_import("core." + mod, entry)
        check("core.%s still resolves" % mod,
              p is not None and p.endswith(os.path.join("core", mod + ".hls")))
    # std.str (hosted) still resolves alongside core.stack
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
# 2. core.stack parses standalone + imports are core-only
# ---------------------------------------------------------------------------

def test_parse() -> None:
    section("2. core.stack parses standalone + core-only imports")
    from boot.lexer import tokenize
    from boot.parser import Parser
    with open(STACK, "rb") as f:
        prog = Parser(tokenize(f.read())).parse_program()
    check("core/stack.hls parses",
          len(prog["structs"]) > 0 and len(prog["enums"]) > 0
          and len(prog["fns"]) > 0,
          "structs=%d, enums=%d, fns=%d"
          % (len(prog["structs"]), len(prog["enums"]), len(prog["fns"])))
    check("StackConfig present", "StackConfig" in prog["structs"])
    check("StackFault enum present", "StackFault" in prog["enums"])
    # Imports: the module needs nothing beyond the core language
    # (no `std.*`, no relative, not even `core.*` — like core.panic).
    imps = [i["path"] for i in prog["imports"]]
    check("imports are core-only",
          all(i.startswith("core.") for i in imps),
          "got: %s" % imps)
    fns = set(prog["fns"].keys())
    must_have = [
        "stack_page_size", "stack_page_align", "stack_page_align_up",
        "stack_pages_for", "stack_fault_name", "stack_fault_code",
        "stack_config_new", "stack_usable_bytes", "stack_guard_bytes",
        "stack_total_footprint_size", "stack_total_footprint",
        "stack_bottom", "stack_guard_base", "stack_offset_valid",
        "stack_classify_offset", "stack_probe", "stack_try_probe",
        "stack_remaining", "stack_has_overflow", "stack_frame_cost",
        "stack_chain_bytes", "stack_max_depth", "stack_chain_fits",
        "stack_plan_new", "stack_canary_value", "stack_canary_ok",
    ]
    missing = [f for f in must_have if f not in fns]
    check("all reference functions present",
          not missing, "missing: %s" % missing)
    check("function count sane (>= 25)", len(fns) >= 25,
          "got %d" % len(fns))


# ---------------------------------------------------------------------------
# 3. Stage-0 enforcement (demo + no_std / freestanding / hosted ok-test)
# ---------------------------------------------------------------------------

def test_enforcement() -> None:
    section("3. Stage-0 enforcement (demo hits the guard; ok-test clean)")
    r = boot("--check", DEMO)
    check("hosted demo checks", r.returncode == 0,
          (r.stdout or "").strip()[:60])
    r = boot(DEMO)
    out = (r.stdout or "") + (r.stderr or "")
    check("demo hits the guard (exit 101)", r.returncode == 101,
          "rc=%d" % r.returncode)
    check("guard message present",
          "guard page hit at offset 8193" in (r.stdout or "")
          or "guard page hit at offset 8193" in (r.stderr or ""))
    check("handler marker on stdout",
          "kernel panic: stack overflow" in (r.stdout or ""))
    check("default report on stderr",
          "guard page hit" in (r.stderr or ""))
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
    # The three Stage 82 fail programs are rejected with the
    # Stage 82 messages (boot side).
    for path, needle in [
            (FAIL_BUDGET, "worst-case stack chain is"),
            (FAIL_RECUR, "requires bounded recursion"),
            (FAIL_FNBODY, "#[stack_size(40)] violated by function 'fat'")]:
        r = boot("--check", path)
        out = (r.stdout or "") + (r.stderr or "")
        check("rejected: %s" % os.path.basename(path),
              r.returncode == 1 and needle in out,
              out.strip()[:100])


# ---------------------------------------------------------------------------
# 4. Targeted probes (interpreter): budget accept/reject + audit
# ---------------------------------------------------------------------------

def test_behavior() -> None:
    section("4. Budget probes (interpreter)")
    # A fitting program: accepted, and --audit reports the chain.
    fits = write_tmp(".hls", """
#![stack_size(4096)]
fn leaf() -> int { return 7 }
fn main() -> int {
    let x: int = leaf()
    let y: int = leaf()
    if x + y != 14 { return 1 }
    return 0
}
""")
    r = boot("--check", fits)
    check("fitting budget accepted", r.returncode == 0,
          ((r.stdout or "") + (r.stderr or "")).strip()[:80])
    r = boot("--audit", fits)
    out = r.stdout or ""
    check("audit reports the budget line",
          "Stack budget: #![stack_size(4096)]" in out
          and "worst chain" in out, out.strip()[-90:])
    check("audit names the worst chain",
          "main -> leaf" in out, "(no path)")
    r = boot(fits)
    check("fitting budget runs (exit 0)", r.returncode == 0,
          "rc=%d" % r.returncode)
    # Over budget: rejected with the chain named.
    over = write_tmp(".hls", """
#![stack_size(100)]
fn leaf() -> int { return 7 }
fn main() -> int {
    let x: int = leaf()
    let y: int = leaf()
    return x + y
}
""")
    r = boot("--check", over)
    out = (r.stdout or "") + (r.stderr or "")
    check("over-budget rejected with the exact chain",
          r.returncode == 1
          and "worst-case stack chain is 112 bytes (main -> leaf)" in out,
          out.strip()[:120])
    # Recursion: rejected regardless of the budget size.
    r = boot("--check", FAIL_RECUR)
    out = (r.stdout or "") + (r.stderr or "")
    check("recursion cycle rejected",
          r.returncode == 1 and "shrink -> shrink" in out,
          out.strip()[:100])
    # The guard probes (each exits 101 with the guard message).
    guard_probes = [
        ("guard: config zero size", """
import "core.stack"
fn main() -> int {
    let _: StackConfig = stack_config_new(0, 1, 1048576)
    return 0
}
"""),
        ("guard: config unaligned size", """
import "core.stack"
fn main() -> int {
    let _: StackConfig = stack_config_new(4097, 1, 1048576)
    return 0
}
"""),
        ("guard: config zero guard pages", """
import "core.stack"
fn main() -> int {
    let _: StackConfig = stack_config_new(4096, 0, 1048576)
    return 0
}
"""),
        ("guard: probe onto the guard page", """
import "core.stack"
fn main() -> int {
    let cfg: StackConfig = stack_config_new(4096, 1, 1048576)
    let _: int = stack_probe(cfg, 4097)
    return 0
}
"""),
        ("guard: remaining past the region", """
import "core.stack"
fn main() -> int {
    let cfg: StackConfig = stack_config_new(4096, 1, 1048576)
    let _: int = stack_remaining(cfg, 4097)
    return 0
}
"""),
        ("guard: zero frame plan", """
import "core.stack"
fn main() -> int {
    let _: StackConfig = stack_plan_new(0, 1, 1, 1048576)
    return 0
}
"""),
    ]
    for name, src in guard_probes:
        p = write_tmp(".hls", src)
        r = boot(p)
        out = (r.stdout or "") + (r.stderr or "")
        check("probe: %s" % name,
              r.returncode == 101 and ("panic" in out or "guard" in out),
              "rc=%d" % r.returncode)


# ---------------------------------------------------------------------------
# 5. Self-hosted emission (hlc) + native parity + runtime machinery
# ---------------------------------------------------------------------------

def test_emission() -> None:
    section("5. Self-hosted emission (hlc) + native parity")
    d = tempfile.mkdtemp(prefix="hls_stack_emit_")
    hlc = os.path.join(REPO_ROOT, "src", "hlc.hls")
    out = os.path.join(d, "sgd.c")
    r = boot(hlc, DEMO, out)
    check("hlc emits hosted C",
          r.returncode == 0 and os.path.isfile(out),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    with open(out, encoding="utf-8", errors="replace") as f:
        src = f.read()
    check("hosted main kept", re.search(r"int main\s*\(", src) is not None)
    check("handler body compiled", "usf_panic_handler" in src)
    check("hook machinery present",
          "hl_panic_hook" in src and "hl_in_panic" in src)
    if subprocess.run([GCC, "-O2", "-o", os.path.join(d, "sgd.bin"),
                        out, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "sgd.bin")],
                             capture_output=True, text=True, timeout=30)
        check("native guard marker",
              "kernel panic: stack overflow" in (nat.stdout or ""),
              "rc=%d" % nat.returncode)
        check("native guard message",
              "guard page hit at offset 8193" in (nat.stdout or "")
              or "guard page hit at offset 8193" in (nat.stderr or ""))
        check("native demo exit 101", nat.returncode == 101,
              "rc=%d" % nat.returncode)
    else:
        check("native stack_guard_demo links", False, "gcc failed")
    # The no_std ok-test compiles to hosted C and exits 0 natively.
    out3 = os.path.join(d, "ok.c")
    r = boot(hlc, OKTEST, out3)
    check("hlc emits no_std ok C", r.returncode == 0
          and os.path.isfile(out3))
    if subprocess.run([GCC, "-O2", "-o", os.path.join(d, "ok.bin"),
                        out3, "-lm", "-pthread"],
                       capture_output=True).returncode == 0:
        nat = subprocess.run([os.path.join(d, "ok.bin")],
                             capture_output=True, timeout=30)
        check("native ok-test exit 0", nat.returncode == 0,
              "rc=%d" % nat.returncode)
    else:
        check("native ok-test links", False, "gcc failed")
    # Freestanding parity: the same ok-test with #![freestanding]
    # must compile to a -nostdlib binary that exits with the same
    # code as the interpreter. Replace ONLY the standalone crate
    # attribute (see test_enforcement for the comment caveat).
    fs_src = open(OKTEST, encoding="utf-8").read().replace(
        "\n#![no_std]\n", "\n#![freestanding]\n", 1)
    fs_p = write_tmp(".hls", fs_src)
    interp = boot(fs_p)
    out2 = os.path.join(d, "fs.c")
    r = boot(hlc, fs_p, out2)
    check("hlc emits freestanding C",
          r.returncode == 0 and os.path.isfile(out2),
          ((r.stdout or "") + (r.stderr or "")).strip()[-100:])
    check("freestanding _start emitted", "void _start" in
          open(out2, encoding="utf-8", errors="replace").read())
    if subprocess.run([GCC, "-O2", "-ffreestanding", "-nostdlib",
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
    # The task runtime carries the deterministic-stack machinery: a
    # program that spawns tasks gets hl_thread_start (pinned 1-MiB
    # stack + guard page) in its emitted C.
    spawner2 = write_tmp(".hls", """
fn work() -> bool {
    return true
}
fn main() -> int uses Conc {
    let t: Task[bool] = spawn(work)
    let r: bool = t.join()
    if r != true { return 1 }
    return 0
}
""")
    outs = os.path.join(d, "spawn.c")
    r = boot(hlc, spawner2, outs)
    check("hlc emits spawn C", r.returncode == 0 and os.path.isfile(outs),
          ((r.stdout or "") + (r.stderr or "")).strip()[-120:])
    if os.path.isfile(outs):
        srcs = open(outs, encoding="utf-8", errors="replace").read()
        check("hl_thread_start present", "hl_thread_start" in srcs)
        check("deterministic stack size pinned",
              "HL_TASK_STACK_BYTES" in srcs)
        check("guard page wired",
              "pthread_attr_setguardsize" in srcs
              and "HL_TASK_GUARD_BYTES" in srcs)
        if subprocess.run([GCC, "-O2", "-o", os.path.join(d, "spawn.bin"),
                            outs, "-lm", "-pthread"],
                           capture_output=True).returncode == 0:
            nat = subprocess.run([os.path.join(d, "spawn.bin")],
                                 capture_output=True, timeout=30)
            check("native spawn task exit 0", nat.returncode == 0,
                  "rc=%d" % nat.returncode)
        else:
            check("native spawn program links", False, "gcc failed")
    # Boot/hlc parity on the fail programs: the self-hosted checker
    # rejects them with the same Stage 82 messages.
    for path, needle in [(FAIL_BUDGET, "worst-case stack chain"),
                         (FAIL_RECUR, "requires bounded recursion"),
                         (FAIL_FNBODY, "violated by function 'fat'")]:
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
    section("6. hlfmt stability over every Stage 82 file")
    import subprocess as sp
    files = [STACK, DEMO, OKTEST, FAIL_BUDGET, FAIL_RECUR, FAIL_FNBODY]
    for f in files:
        r = sp.run([PY, os.path.join(REPO_ROOT, "tools", "hlfmt.py"),
                    "-c", f], capture_output=True, text=True,
                   cwd=REPO_ROOT, timeout=120)
        check("hlfmt clean: %s" % os.path.basename(f),
              "NOT formatted" not in (r.stdout or ""),
              (r.stdout or "").strip()[:80])


# ---------------------------------------------------------------------------
# 7. --audit shows every core.stack function pure
# ---------------------------------------------------------------------------

def test_audit() -> None:
    section("7. --audit: every core.stack function pure (no uses)")
    r = boot("--audit", OKTEST)
    out = r.stdout or ""
    check("audit ran", r.returncode == 0)
    must_be_pure = [
        "stack_page_size", "stack_page_align", "stack_page_align_up",
        "stack_pages_for", "stack_fault_name", "stack_fault_code",
        "stack_config_new", "stack_usable_bytes", "stack_guard_bytes",
        "stack_total_footprint_size", "stack_total_footprint",
        "stack_bottom", "stack_guard_base", "stack_offset_valid",
        "stack_classify_offset", "stack_probe", "stack_try_probe",
        "stack_remaining", "stack_has_overflow", "stack_frame_cost",
        "stack_chain_bytes", "stack_max_depth", "stack_chain_fits",
        "stack_plan_new", "stack_canary_value", "stack_canary_ok",
    ]
    missing = [f for f in must_be_pure if f not in out]
    check("all stack fns in audit output", not missing,
          "missing: %s" % missing[:5])
    bad = []
    for line in out.splitlines():
        s = line.strip()
        for f in must_be_pure:
            if s.startswith(f + " "):
                if "(none - pure)" not in s and "(none)" not in s:
                    bad.append(s)
                break
    check("all stack fns declared pure", not bad,
          "impure: %s" % bad[:3])
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
        print("STACK ACCEPTANCE: %d assertion(s) FAILED" % failed)
        return 1
    print("ACCEPTANCE OK: Stage 82 -- deterministic stack size + guard pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
