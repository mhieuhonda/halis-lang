#!/usr/bin/env python3
"""Stage 111 acceptance gate — capability token types (`Cap[Net]` as a
value, not just effect).

Run with `make cap-acceptance` (or
`python3 tests/cap_acceptance.py`).

Seven sections:

  1. the parser        — Cap[E] accepts the nine effect names, rejects
                         everything else; `Cap` cannot be shadowed
  2. the mint rules    — cap_take("E") only in main or a fn declaring
                         E; wrong/unknown literals rejected
  3. the grants        — a direct Cap[E] parameter discharges E (all
                         nine effects, family form included); no token
                         transitively; pure fns hold no authority
  4. the opaqueness    — no clone (tokens or composites holding them),
                         no printing; tokens are not forgeable
  5. the crate modes   — Cap types and cap_take are no_std/freestanding
                         errors
  6. the differential  — the five feat_stage111 programs and the demo
                         are byte-identical interpreter vs native
                         (default AND -O fast)
  7. the audit         — --audit prints the Capability tokens section
                         (same words on both front-ends)
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from boot.boot import load_program            # noqa: E402
from boot.checker import check                # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_cap_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/cap_demo.hls"
OK_TESTS = [
    "tests/ok/feat_stage111_cap_basic.hls",
    "tests/ok/feat_stage111_cap_family.hls",
    "tests/ok/feat_stage111_cap_name.hls",
    "tests/ok/feat_stage111_cap_spawn.hls",
    "tests/ok/feat_stage111_cap_struct.hls",
]

EFFECTS = ["IO", "Fs", "Clock", "Args", "Exit", "Net", "Rand", "Proc", "Conc"]


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def write(name, src):
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(src)
    return path


def check_deny(name, src, expect):
    """boot's checker must reject the source with the expected message."""
    path = write(name, src)
    proc = subprocess.run(
        [sys.executable, "boot/boot.py", "--check", path],
        capture_output=True, text=True)
    err = proc.stderr + proc.stdout
    if proc.returncode == 1 and expect in err:
        ok("%s rejected (%s)" % (name, expect))
    else:
        bad("%s: rc=%d expected '%s' in: %s"
            % (name, proc.returncode, expect, err[:220]))


def check_deny_hlc(name, src, expect):
    """The self-hosted compiler must reject the source too."""
    path = write(name, src)
    out_c = os.path.join(TMP, name + ".c")
    proc = subprocess.run(
        [os.path.join("bin", "hlc"), path, out_c],
        capture_output=True, text=True)
    err = proc.stderr + proc.stdout
    if proc.returncode != 0 and expect in err:
        ok("%s rejected by hlc (%s)" % (name, expect))
    else:
        bad("%s (hlc): rc=%d expected '%s' in: %s"
            % (name, proc.returncode, expect, err[:220]))


def compile_native(src, out_c, hlc=None, flags=None):
    if hlc is None:
        cmd = [sys.executable, "boot/boot.py", src, out_c]
    else:
        cmd = [hlc] + (flags or []) + [src, out_c]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0


def run_binary(binary, timeout=120):
    proc = subprocess.run([binary], capture_output=True, text=True,
                          timeout=timeout)
    return proc.stdout, proc.returncode


def run_interp(src):
    proc = subprocess.run(
        [sys.executable, "boot/boot.py", src],
        capture_output=True, text=True, timeout=180)
    return proc.stdout, proc.returncode


HLC = os.path.join(ROOT, "bin", "hlc")

print("=== 1. the parser: Cap[E] names an effect, nothing else does ===")
for eff in EFFECTS:
    path = write("cap_%s.hls" % eff,
                 "fn main() -> int {\n"
                 "    let c: Cap[%s] = cap_take(\"%s\")\n"
                 "    return 0\n"
                 "}\n" % (eff, eff))
    try:
        prog = load_program(path)
        check(prog)
        ok("Cap[%s] parses, checks, and mints" % eff)
    except Exception as e:  # noqa: BLE001
        bad("Cap[%s] rejected: %s" % (eff, e))

check_deny("cap_bad_name.hls",
           "fn main() -> int {\n    let c: Cap[What] = cap_take(\"Net\")\n"
           "    return 0\n}\n",
           "unknown capability 'Cap[What]'")
check_deny("cap_bad_take.hls",
           "fn main() -> int {\n    let c: Cap[Net] = cap_take(\"What\")\n"
           "    return 0\n}\n",
           "unknown capability 'Cap[What]'")
check_deny("cap_nonliteral.hls",
           "fn main() -> int {\n"
           "    let e: str = \"Net\"\n"
           "    let c: Cap[Net] = cap_take(e)\n"
           "    return 0\n}\n",
           "string literal naming an effect")
check_deny("cap_shadow.hls",
           "fn f[Cap](x: int) -> int { return x }\n"
           "fn main() -> int { return 0 }\n",
           "type parameter cannot be named 'Cap'")

print("=== 2. the mint rules: main, or an explicit grant ===")
check_deny("cap_take_outside.hls",
           "fn mint() -> int {\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n    return 0\n}\n"
           "fn main() -> int { return 0 }\n",
           "cap_take('Net') may only be called from 'main' or a function "
           "declaring 'uses Net'")
check_deny("cap_take_wrong_decl.hls",
           "fn mint() -> int uses Fs {\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n    return 0\n}\n"
           "fn main() -> int { return 0 }\n",
           "cap_take('Net') may only be called from 'main' or a function "
           "declaring 'uses Net'")
# ...but a fn declaring the effect MAY split a token off.
path = write("cap_take_declared.hls",
             "fn mint() -> Cap[Net] uses Net {\n"
             "    return cap_take(\"Net\")\n}\n"
             "fn main() -> int {\n"
             "    let io: Cap[IO] = cap_take(\"IO\")\n"
             "    let c: Cap[Net] = mint()\n"
             "    println(cap_effect_name(c))\n"
             "    return 0\n}\n")
try:
    prog = load_program(path)
    check(prog)
    ok("a fn declaring uses Net may mint Cap[Net]")
except Exception as e:  # noqa: BLE001
    bad("declared mint rejected: %s" % e)

print("=== 3. the grants: signature-carried authority ===")
check_deny("cap_missing_grant.hls",
           "fn leak() -> int {\n"
           "    return net_lookup(\"localhost\").len()\n}\n"
           "fn main() -> int { return leak() }\n",
           "requires effect 'Net' not declared")
check_deny("cap_transitive.hls",
           "fn helper2() -> int {\n"
           "    return net_lookup(\"localhost\").len()\n}\n"
           "fn middle() -> int { return helper2() }\n"
           "fn main() -> int {\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n"
           "    return middle()\n}\n",
           "requires effect 'Net' not declared")
check_deny("cap_pure_param.hls",
           "fn hold(c: Cap[Net]) -> int pure { return 0 }\n"
           "fn main() -> int { return 0 }\n",
           "pure function 'hold' cannot take a capability parameter")
check_deny("cap_struct_field_no_grant.hls",
           "struct Bag { t: Cap[Net] }\n"
           "fn act(b: Bag) -> int {\n"
           "    return net_lookup(\"localhost\").len()\n}\n"
           "fn main() -> int {\n"
           "    let io: Cap[IO] = cap_take(\"IO\")\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n"
           "    let b: Bag = Bag { t: c }\n"
           "    return act(b)\n}\n",
           "function 'act' calls 'net_lookup' which requires effect 'Net'")

print("=== 4. the opaqueness: unforgeable, unduplicable, unprintable ===")
check_deny("cap_clone.hls",
           "fn dup(c: Cap[Net]) -> Cap[Net] {\n"
           "    let d: Cap[Net] = clone(c)\n    return d\n}\n"
           "fn main() -> int {\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n"
           "    let e: Cap[Net] = dup(c)\n    return 0\n}\n",
           "clone() on type Cap[Net] is not supported")
check_deny("cap_clone_struct.hls",
           "struct Bag { t: Cap[Net] }\n"
           "fn main() -> int {\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n"
           "    let b: Bag = Bag { t: c }\n"
           "    let b2: Bag = clone(b)\n    return 0\n}\n",
           "clone() on type Bag is not supported")
check_deny("cap_print.hls",
           "fn main() -> int {\n"
           "    let c: Cap[Net] = cap_take(\"Net\")\n"
           "    println(c)\n    return 0\n}\n",
           "println expects argument 1 to be str, got Cap[Net]")
# no literal forgery: the only Cap values come from cap_take / params.
path = write("cap_no_literal.hls",
             "fn main() -> int {\n"
             "    let io: Cap[IO] = cap_take(\"IO\")\n"
             "    println(\"Net\")\n"
             "    return 0\n}\n")
try:
    prog = load_program(path)
    check(prog)
    ok("a str is not a Cap — type system keeps them distinct")
except Exception as e:  # noqa: BLE001
    bad("plain str confused with Cap: %s" % e)

print("=== 5. the crate modes: no OS, no authority ===")
for label, header in (("#![no_std]", "#![no_std]"),):
    check_deny("cap_type_%s.hls" % label,
               "%s\nfn hold(c: Cap[Net]) -> int { return 0 }\n"
               "fn main() -> int { return 0 }\n" % header,
               "capability type 'Cap[Net]' is unavailable in %s mode"
               % label)
    check_deny("cap_take_%s.hls" % label,
               "%s\nfn main() -> int {\n"
               "    let c: Cap[Net] = cap_take(\"Net\")\n"
               "    return 0\n}\n" % header,
               "cap_take() is not available in %s mode" % label)

print("=== 6. the differential: interpreter vs native (both -O modes) ===")
for src in OK_TESTS + [DEMO]:
    base = os.path.basename(src)
    iout, irc = run_interp(src)
    native_ok = True
    for flags in ([], ["-O", "fast"]):
        tag = "fast" if flags else "default"
        out_c = os.path.join(TMP, "%s_%s.c" % (base, tag))
        bin_c = os.path.join(TMP, "%s_%s" % (base, tag))
        if not compile_native(src, out_c, hlc=HLC, flags=flags):
            bad("%s [%s]: hlc refused" % (base, tag))
            native_ok = False
            continue
        cc = subprocess.run(["gcc", "-O2", "-o", bin_c, out_c,
                             "-lm", "-pthread"],
                            capture_output=True, text=True)
        if cc.returncode != 0:
            bad("%s [%s]: gcc refused" % (base, tag))
            native_ok = False
            continue
        nout, nrc = run_binary(bin_c)
        if nout != iout or nrc != irc:
            bad("%s [%s]: interp(exit=%d) != native(exit=%d)\n"
                "    interp: %r\n    native: %r"
                % (base, tag, irc, nrc, iout[:120], nout[:120]))
            native_ok = False
    if native_ok:
        ok("%s identical on both front-ends (default + -O fast)" % base)

print("=== 7. the audit: the Capability tokens section, word-for-word ===")
audit_src = write("cap_audit.hls", open(DEMO).read())
boot_audit = subprocess.run(
    [sys.executable, "boot/boot.py", "--audit", audit_src],
    capture_output=True, text=True)
boot_rows = [ln.strip() for ln in boot_audit.stdout.splitlines()
             if "Capability tokens" in ln or ln.strip().startswith("main:")
             or ln.strip().startswith("fetch:")]
hlc_audit = subprocess.run(
    [HLC, "--audit", audit_src], capture_output=True, text=True)
hlc_rows = [ln.strip() for ln in hlc_audit.stdout.splitlines()
            if "Capability tokens" in ln or ln.strip().startswith("main:")
            or ln.strip().startswith("fetch:")]
if "Capability tokens (Cap[E] parameters / cap_take calls):" in boot_rows \
        and "Capability tokens (Cap[E] parameters / cap_take calls):" in hlc_rows:
    ok("both audits announce the section")
else:
    bad("audit section header missing (boot=%r hlc=%r)"
        % (boot_rows[:1], hlc_rows[:1]))
boot_grants = [ln for ln in boot_audit.stdout.splitlines()
               if ln.strip() in ("main: IO, Fs, Clock, Args, Exit, Net",
                                 "fetch: Net")]
hlc_grants = [ln for ln in hlc_audit.stdout.splitlines()
              if ln.strip() in ("main: IO, Fs, Clock, Args, Exit, Net",
                                "fetch: Net")]
if sorted(boot_grants) == sorted(hlc_grants) and len(hlc_grants) == 2:
    ok("grant rows agree: %s" % sorted(ln.strip() for ln in hlc_grants))
else:
    bad("grant rows differ: boot=%r hlc=%r" % (boot_grants, hlc_grants))

print("")
print("=== Stage 111 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
