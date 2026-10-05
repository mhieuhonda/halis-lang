#!/usr/bin/env python3
"""Stage 110 acceptance gate — sandboxed execution, seccomp-bpf.

Run with `make sandbox-acceptance` (or
python3 tests/sandbox_acceptance.py).

Nine sections:

  1. the one definition — the vocabulary IS hls-audit's, the fs
                          census IS BUILTIN_EFFECTS' Fs family, the
                          derivation walks with audit's own helpers,
                          the CLI selftest (37 vectors) is green
  2. the assembler      — pinned program shapes, determinism, the
                          merge semantics (rw absorbs ro), the
                          jump fixups, the disassembler, the
                          over-range refusal
  3. the policy         — the layer invariant on all three arches,
                          the absent-notes, the x86_64 numbers
                          re-checked against the local kernel
                          headers when they exist, the [sandbox]
                          section's validation, the deny rules
  4. the derivation     — source mode on the demo (the surface, the
                          census, the honest rw), package mode on
                          the package demo (the gate, the [sandbox]
                          pins), the gate refusing a tree that
                          violates its own manifest, --effects as
                          the yardstick override
  5. the enforcement    — the kernel half, Linux + seccomp only
                          (skip-marked elsewhere): the demo compiled
                          native (boot.py -> C -> cc), the matrix
                          ro/rw/kill, the loader line (minimal
                          baseline refuses a dynamic artifact), the
                          static line, the net probe (the declaration
                          lied, the kernel did not), the package-mode
                          run, the exit pass-through
  6. the shim           — emit + compile the self-armed binary (no
                          launcher), the ro refusal baked in, the
                          cross-arch emission (aarch64 + riscv64
                          headers, compiled for the host arch)
  7. the statement      — the release gate (no lockfile refuses),
                          the versioned statement, ONE ledger record
                          (kind sandbox), drift refusal, the tampered
                          statement refusal, verify-release end to
                          end
  8. the demos          — the demo and the ok-test run under the
                          interpreter, native parity byte for byte,
                          the package demo runs
  9. the tools          — hlfmt stable, hllint clean on the new
                          sources
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

SBX = [sys.executable, os.path.join(ROOT, "tools", "hls-sandbox.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]
HLC = os.path.join(ROOT, "src", "hlc.hls")
DEMO = "examples/sandbox_demo.hls"
PKG_DEMO = "examples/pkg_sandbox_demo"
OK_TEST = "tests/ok/feat_stage110_sandbox.hls"

PASS = 0
FAIL = 0
SKIP = 0
TMP = tempfile.mkdtemp(prefix="_gate_s110_", dir=os.path.join(ROOT, "tests"))
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN, exist_ok=True)
LOG_MAIN = os.path.join(ROOT, ".hls-pkg-transparency.log")


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def skip(msg):
    global SKIP
    SKIP += 1
    print("  [SKIP] %s" % msg)


def expect(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


# ---------------------------------------------------------------------------
print("=== 1. the one definition ===")
# ---------------------------------------------------------------------------

from sandbox_parts import sbx_policy as P                    # noqa: E402
from sandbox_parts import sbx_exec, sbx_cemit, sbx_release   # noqa: E402
from sandbox_parts.sbx_policy import _SYSCALLS               # noqa: E402

_h = run(SBX + ["selftest"])
expect(_h.returncode == 0
       and "37 passed / 0 failed" in _h.stdout,
       "the CLI selftest is green (37 vectors)")

expect(sorted(P.ALL_EFFECTS) == sorted(
    ["IO", "Fs", "Clock", "Args", "Exit", "Net", "Rand", "Proc", "Conc"]),
    "the effect vocabulary is the audit's nine (one set, imported)")

_mod_file = os.path.join(ROOT, "tools", "sandbox_parts", "sbx_policy.py")
_src = open(_mod_file).read()
expect("hls_audit._walk_import_graph" in _src
       and "hls_audit._check_merged" in _src
       and "hls_audit._fn_keys_per_module" in _src,
       "the derivation walks with hls-audit's own helpers (no re-walk)")

from boot.checking.helpers import BUILTIN_EFFECTS            # noqa: E402
_fs = {b for b, e in BUILTIN_EFFECTS.items() if e == {"Fs"}}
expect(set(P.FS_READ_BUILTINS) | set(P.FS_WRITE_BUILTINS) <= _fs,
       "the fs census is the Fs family of the one builtin->effect table")
expect(not (set(P.FS_READ_BUILTINS) & set(P.FS_WRITE_BUILTINS)),
       "the fs census splits Fs without overlap")

# ---------------------------------------------------------------------------
print("=== 2. the assembler ===")
# ---------------------------------------------------------------------------

pure = P.Profile([], None, "gate", "errno", "minimal")
prog = pure.program("x86_64")
expect(len(prog) == 29, "the pure minimal program is 29 instructions")
expect(prog.bytes() == pure.program("x86_64").bytes()
       and pure.program("x86_64").bytes()
       == P.Profile([], None, "gate2", "errno", "minimal")
       .program("x86_64").bytes(),
       "assembly is deterministic across instances")

fr = P.Profile(["IO", "Fs"], "ro", "g", "errno", "hosted")
fw = P.Profile(["IO", "Fs"], "rw", "g", "errno", "hosted")
expect(len(fr.program("x86_64")) == 93 and len(fw.program("x86_64")) == 109,
       "IO+Fs(ro) hosted assembles to 93 instructions, rw to 109")
expect(len(fr.rules("x86_64")) == 41 and len(fw.rules("x86_64")) == 52,
       "ro allows 41 syscalls, rw 52 — the write side is the difference")

rules = fr.rules("x86_64")
_openat_ro = [r for r in rules.values() if r.name == "openat"][0]
_expect_cons = {(2, P.OPEN_WRITE_MASK)}
expect(set(_openat_ro.constraints) == _expect_cons
       and not _openat_ro.unconditional,
       "Fs(ro) constrains openat's flags (arg2) with the write mask")
rules_w = fw.rules("x86_64")
_openat_w = [r for r in rules_w.values() if r.name == "openat"][0]
expect(_openat_w.unconditional,
       "Fs(rw) upgrades openat to unconditional (rw absorbs ro)")
_o_x = [r for r in rules_w.values() if r.name == "open"][0]
expect(_o_x.unconditional,
       "Fs(rw) upgrades plain open too (the glibc fopen detail)")

from sandbox_parts import sbx_bpf as bpf                     # noqa: E402
_names = {rules[nr].nr: rules[nr].name for nr in rules}
_txt = bpf.disassemble(fr.program("x86_64"), _names)
expect("jeq    257   openat" in _txt and "jset   0x410643" in _txt,
       "the listing annotates the dispatch and the write mask")

big = bpf.Asm()
big.load(bpf.OFF_NR)
for i in range(300):
    big.jeq(i, jt_label="f%d" % i)
big.ret(bpf.SECCOMP_RET_ALLOW)
for i in range(300):
    big.label("f%d" % i)
    big.ret(bpf.SECCOMP_RET_ALLOW)
try:
    big.finish()
    bad("an over-range jump is refused (8-bit jt/jf)")
except ValueError as ex:
    ok("an over-range jump is refused (8-bit jt/jf)")

# ---------------------------------------------------------------------------
print("=== 3. the policy ===")
# ---------------------------------------------------------------------------

expect(all(v[1] == v[2] for v in _SYSCALLS.values()),
       "aarch64 and riscv64 share the asm-generic numbering (every row)")
_missing = []
for eff, entries in P.LAYERS.items():
    for entry in entries:
        name = entry[0] if isinstance(entry, tuple) else entry
        for arch in ("x86_64", "aarch64", "riscv64"):
            nr, note = P.nr_of(name, arch)
            if nr is None and note is None:
                _missing.append((eff, name, arch))
expect(not _missing,
       "every layer syscall resolves on all three arches or carries its reason")
expect(P.nr_of("stat", "x86_64")[0] == 4
       and P.nr_of("stat", "aarch64")[0] is None,
       "plain stat is x86_64-only with the recorded reason")

# The x86_64 numbers, re-checked against THIS machine's kernel headers
# when they exist (the numbers are ABI; the check is honesty).
_hdr = None
for _c in ("/usr/include/x86_64-linux-gnu/asm/unistd_64.h",
           "/usr/include/i386-linux-gnu/asm/unistd_64.h",
           "/usr/include/asm/unistd_64.h"):
    if os.path.isfile(_c):
        _hdr = _c
        break
if _hdr is None:
    skip("no x86_64 unistd_64.h on this machine — header cross-check")
else:
    _want = {"read": 0, "write": 1, "openat": 257, "open": 2,
             "exit": 60, "exit_group": 231, "brk": 12, "execve": 59,
             "seccomp": 317, "clone": 56, "clone3": 435,
             "fchmodat": 268, "statx": 332, "rseq": 334}
    _txt_h = open(_hdr).read()
    _bad = [n for n, v in _want.items()
            if ("__NR_%s %d" % (n, v)) not in _txt_h]
    expect(not _bad,
           "the table's x86_64 numbers match the kernel headers (%s)"
           % os.path.basename(_hdr))

for _sec, _want_err in (
        ({"sandbox": {"default": "teleport"}}, "default"),
        ({"sandbox": {"fs": "rx"}}, "fs"),
        ({"sandbox": {"allow": ["nope"]}}, "unknown syscall"),
        ({"sandbox": {"deny": ["exit_group"]}}, "hang"),
        ({"sandbox": "flat"}, "must be a table")):
    try:
        P.parse_sandbox_section(_sec)
        bad("[sandbox] %s is refused" % _want_err)
    except P.PolicyError as ex:
        ok("[sandbox] %s is refused (%s...)"
           % (_want_err, str(ex)[:34]))

try:
    P.Profile(["Bogus"], None, "g", "errno", "hosted")
    bad("an unknown effect is refused")
except P.PolicyError:
    ok("an unknown effect is refused")

_deny_execve = P.Profile(["IO"], None, "g", "errno", "hosted",
                         deny=["execve"])
expect("execve" not in [r.name for r in
                        _deny_execve.rules("x86_64").values()],
       "deny on execve builds a profile (the shim's line)")
try:
    sbx_exec.run(_deny_execve, "x86_64", ["/bin/echo", "x"])
    bad("the launcher refuses an execve-denying profile (it must cross)")
except P.PolicyError:
    ok("the launcher refuses an execve-denying profile (it must cross)")

expect(P.is_static_elf("/bin/sh") is False,
       "the ELF probe reads /bin/sh as dynamic")

# ---------------------------------------------------------------------------
print("=== 4. the derivation ===")
# ---------------------------------------------------------------------------

_d = run(SBX + ["profile", DEMO, "--json"])
expect(_d.returncode == 0, "profile runs on the demo (source mode)")
_rep = json.loads(_d.stdout)
expect(_rep["effects"] == ["Args", "Fs", "IO"]
       and _rep["derivation"]["builtins"]
       == ["args", "println", "read_file", "write_file"],
       "the derived surface and the reachable census are pinned")
expect(_rep["fs"]["mode"] == "rw"
       and "write side" in _rep["fs"]["origin"],
       "the demo derives fs=rw (write_file is reachable — static and "
       "conservative)")

_d2 = run(SBX + ["profile", DEMO, "--fs", "ro", "--json"])
_rep2 = json.loads(_d2.stdout)
expect(_rep2["fs"]["mode"] == "ro" and _rep2["fs"]["origin"] == "--fs",
       "--fs ro tightens below the derivation, and the report says so")

_pk = run(SBX + ["profile", "--pkg", PKG_DEMO, "--json"])
expect(_pk.returncode == 0, "profile runs on the package demo")
_prep = json.loads(_pk.stdout)
expect(_prep["effects"] == ["Clock", "Fs", "IO"]
       and _prep["fs"]["mode"] == "ro"
       and _prep["denied"] == ["clone", "fork", "vfork"]
       and _prep["gate"]["violations"] == [],
       "package mode: the gate passes, [sandbox] pins ro + the denies")

# The gate refuses a tree that violates its own manifest.
_vio = os.path.join(TMP, "violating")
_vio_dep = os.path.join(TMP, "violating_dep")
os.makedirs(_vio, exist_ok=True)
os.makedirs(_vio_dep, exist_ok=True)
open(os.path.join(_vio_dep, "hls-pkg.toml"), "w").write(
    '[package]\nname = "vio_dep"\nversion = "0.1.0"\n')
open(os.path.join(_vio_dep, "main.hls"), "w").write(
    'fn noisy() -> int uses Net {\n'
    '    let fd: int = net_udp_open()\n'
    '    return fd\n'
    '}\n')
open(os.path.join(_vio, "hls-pkg.toml"), "w").write(
    '[package]\nname = "vio_root"\nversion = "0.1.0"\n\n'
    '[dependencies]\nvio_dep = { path = "%s" }\n\n'
    '[effects]\nallowed = ["IO"]\n'
    % os.path.relpath(_vio_dep, ROOT))
open(os.path.join(_vio, "main.hls"), "w").write(
    'fn main() -> int uses IO {\n    println("vio")\n    return 0\n}\n')
_v = run(SBX + ["run", "--pkg", _vio, "--", "/bin/true"])
expect(_v.returncode == 1 and "audit gate" in _v.stderr,
       "the gate refuses to arm a tree that violates its own manifest")
_v2 = run(SBX + ["run", "--pkg", _vio, "--effects", "IO,Net",
                 "--", "/bin/true"])
expect(_v2.returncode == 0,
       "--effects widens the GATE's yardstick (the hls-audit --allow "
       "spelling) and the run proceeds")

# ---------------------------------------------------------------------------
print("=== 5. the enforcement ===")
# ---------------------------------------------------------------------------

_arch = P.arch_of_host()
_ok_arm, _why = sbx_exec.seccomp_supported(_arch or "x86_64")
_have_cc = shutil.which("cc") or shutil.which("gcc")

if _arch is None or not _ok_arm or not _have_cc:
    skip("the enforcement matrix needs Linux seccomp + a C compiler "
         "(arch=%r, arm=%s, cc=%s)" % (_arch, _ok_arm, bool(_have_cc)))
else:
    _c = os.path.join(TMP, "sandbox_demo.c")
    _b = os.path.join(BIN, "sandbox_demo")
    _rc1 = run(BOOT + [HLC, DEMO, _c])
    _rc2 = run(["cc", "-O2", "-o", _b, _c, "-lm", "-pthread"])
    expect(_rc1.returncode == 0 and _rc2.returncode == 0,
           "the demo compiles native (boot.py -> C -> cc)")

    def sbx_run(effects, fs, cmd, extra=None, default=None):
        a = SBX + ["run", "--effects", effects, "--fs", fs,
                   "--baseline", "hosted"]
        if default:
            a += ["--default", default]
        a += (extra or []) + ["--"] + cmd
        return run(a)

    r = sbx_run("Args,Fs,IO", "ro", [_b])
    expect(r.returncode == 0 and "digest 893425381" in r.stdout,
           "confined ro: the read runs, the digest matches")
    r = sbx_run("Args,Fs,IO", "ro", [_b, "write"])
    expect(r.returncode == 101 and "cannot write file" in r.stderr,
           "confined ro: the write is refused by the KERNEL and "
           "surfaces as the language's own panic (101)")
    r = sbx_run("Args,Fs,IO", "rw", [_b, "write"])
    expect(r.returncode == 0 and "wrote the bytes back" in r.stdout,
           "confined rw: the legitimate write is not broken")
    r = sbx_run("Args,Fs,IO", "ro", [_b, "write"], default="kill")
    expect(r.returncode == 159 and r.stderr.find("SIGSYS") >= 0,
           "kill mode: the kernel ends the process at the denied "
           "syscall (159 = 128+31)")

    # The loader line: a dynamic artifact under the minimal baseline.
    r = run(SBX + ["run", "--effects", "IO", "--baseline", "minimal",
                   "--", _b])
    expect(r.returncode in (127, 125)
           and ("loading shared libraries" in r.stderr
                or "cannot" in r.stderr),
           "minimal baseline + dynamic artifact: the loader is refused "
           "(the auto baseline exists for this)")

    # The static line: -static needs nothing but the minimal set.
    _sb = os.path.join(BIN, "sandbox_demo_static")
    _rcs = run(["cc", "-O2", "-static", "-o", _sb, _c, "-lm",
                "-pthread"])
    if _rcs.returncode != 0:
        skip("no static glibc on this machine — the static line")
    else:
        r = run(SBX + ["run", "--effects", "Args,Fs,IO", "--fs", "ro",
                       "--baseline", "auto", "--", _sb])
        expect(r.returncode == 0 and "digest 893425381" in r.stdout,
               "auto baseline reads the static ELF and the minimal "
               "set carries it")

    # The lie: the profile says no Net, the program asks for a socket.
    _np_h = os.path.join(TMP, "net_probe.hls")
    _np_c = os.path.join(TMP, "net_probe.c")
    _np = os.path.join(BIN, "net_probe")
    open(_np_h, "w").write(
        'fn main() -> int uses Net, IO {\n'
        '    let fd: int = net_udp_open()\n'
        '    if fd >= 0 {\n'
        '        println("net_probe: socket created - the kernel '
        'allowed it")\n'
        '        net_close(fd)\n'
        '    } else {\n'
        '        println("net_probe: socket refused - the kernel said '
        'no")\n'
        '    }\n'
        '    return 0\n'
        '}\n')
    run(BOOT + [HLC, _np_h, _np_c])
    run(["cc", "-O2", "-o", _np, _np_c, "-lm", "-pthread"])
    r_plain = run([_np])
    r_conf = run(SBX + ["run", "--effects", "IO", "--", _np])
    expect(r_plain.returncode == 0
           and "socket created" in r_plain.stdout,
           "unconfined, the probe opens its socket")
    expect(r_conf.returncode == 0
           and "socket refused - the kernel said no" in r_conf.stdout,
           "confined to IO, the probe's socket is refused — the "
           "declaration lied, the kernel did not")

    # The exit contract passes the artifact's own code through.
    r = run(SBX + ["run", "--effects", "IO", "--", "/bin/sh", "-c",
                   "exit 7"])
    expect(r.returncode == 7, "the artifact's own exit code passes "
                              "through the launcher")

    # Package mode, end to end: profile -> run.
    _pc = os.path.join(TMP, "pkg_demo.c")
    _pb = os.path.join(BIN, "pkg_demo")
    run(BOOT + [HLC, os.path.join(PKG_DEMO, "main.hls"), _pc])
    run(["cc", "-O2", "-o", _pb, _pc, "-lm", "-pthread"])
    r = run(SBX + ["run", "--pkg", PKG_DEMO, "--", _pb])
    expect(r.returncode == 0 and "digest 893425381" in r.stdout,
           "package mode: the gated profile carries the run")

# ---------------------------------------------------------------------------
print("=== 6. the shim ===")
# ---------------------------------------------------------------------------

if _arch is None or not _have_cc:
    skip("the shim compile needs a C compiler on a known arch")
else:
    _shim = os.path.join(TMP, "shim_ro.c")
    _e = run(SBX + ["emit", DEMO, "--fs", "ro", "--arch", "x86_64",
                    "--out", _shim])
    expect(_e.returncode == 0 and os.path.isfile(_shim),
           "emit writes the self-arming shim")
    _sc = open(_shim).read()
    expect("constructor(101)" in _sc
           and "PR_SET_NO_NEW_PRIVS" in _sc
           and "SECCOMP_SET_MODE_FILTER" in _sc,
           "the shim arms in a priority constructor: NO_NEW_PRIVS, "
           "then the filter")
    _selfarmed = os.path.join(BIN, "demo_shimmed")
    _rcc = run(["cc", "-O2", "-o", _selfarmed,
                os.path.join(TMP, "sandbox_demo.c"), _shim,
                "-lm", "-pthread"])
    expect(_rcc.returncode == 0,
           "the shim compiles into the binary (no launcher needed)")
    r = run([_selfarmed])
    expect(r.returncode == 0 and "digest 893425381" in r.stdout,
           "the self-armed binary runs its read unconfined-by-launcher")
    r = run([_selfarmed, "write"])
    expect(r.returncode == 101 and "cannot write file" in r.stderr,
           "the self-armed binary refuses its own write (the ro "
           "policy is baked in)")

    for _a, _magic in (("aarch64", "0xc00000b7"),
                       ("riscv64", "0xc00000f3")):
        _x = os.path.join(TMP, "shim_%s.c" % _a)
        _rx = run(SBX + ["emit", "--effects", "IO", "--arch", _a,
                         "--out", _x])
        _txt_x = open(_x).read() if _rx.returncode == 0 else ""
        expect(_rx.returncode == 0 and _magic in _txt_x,
               "emit --arch %s pins its own AUDIT_ARCH (%s)"
               % (_a, _magic))

# ---------------------------------------------------------------------------
print("=== 7. the statement ===")
# ---------------------------------------------------------------------------

_rel = os.path.join(TMP, "rel_sbx")
shutil.copytree(PKG_DEMO, _rel)
_mp = os.path.join(_rel, "hls-pkg.toml")
_s = open(_mp).read()
open(_mp, "w").write(_s.replace(
    'path = "examples/pkg_sandbox_demo/libs/sandbox_lib/main.hls"',
    'path = "%s"' % os.path.relpath(
        os.path.join(_rel, "libs", "sandbox_lib", "main.hls"), ROOT)))

_lines_before = 0
if os.path.exists(LOG_MAIN):
    with open(LOG_MAIN, "rb") as f:
        _lines_before = len(f.readlines())

_r0 = run(SBX + ["release", "--pkg", _rel])
expect(_r0.returncode == 1 and "lockfile" in _r0.stderr,
       "release without a lockfile refuses (the family contract)")

_lp = run(PKG + ["lock"], cwd=_rel)
expect(_lp.returncode == 0, "hls-pkg lock pins the release fixture")

_r1 = run(SBX + ["release", "--pkg", _rel])
_st = os.path.join(_rel, "hls-sandbox-pkg_sandbox_demo-0.1.0"
                   ".profile.json")
expect(_r1.returncode == 0 and os.path.isfile(_st),
       "the locked tree releases the versioned statement")
_stmt = json.load(open(_st))
expect(_stmt["schema"] == "hls-sandbox-statement/v1"
       and _stmt["effects"] == ["Clock", "Fs", "IO"]
       and _stmt["fs"]["mode"] == "ro"
       and _stmt["deny"] == ["clone", "fork", "vfork"],
       "the statement pins the posture (effects, fs, denies)")
expect(_stmt["constrained"] == ["open", "openat"],
       "the statement names the two write-masked syscalls")

with open(LOG_MAIN, "rb") as f:
    _lines = f.readlines()
expect(len(_lines) == _lines_before + 2,
       "the failed-then-succeeded pair appended records; the release "
       "itself chains exactly one")
_rec = json.loads(_lines[-1].decode("utf-8"))
expect(_rec.get("kind") == "sandbox"
       and _rec.get("name") == "pkg_sandbox_demo"
       and _rec.get("fs") == "ro",
       "the ledger record is kind sandbox with the posture's summary")

_vr = run(SBX + ["verify-release", "--pkg", _rel])
expect(_vr.returncode == 0 and "VERIFIED" in _vr.stdout
       and _vr.stdout.count("ledger:") == 1,
       "verify-release passes end to end on the untouched tree")

# Drift refuses: change the dep under the lock.
_dp = os.path.join(_rel, "libs", "sandbox_lib", "main.hls")
with open(_dp, "a") as f:
    f.write("\nfn extra() -> int uses Fs {\n"
            '    return fs_size("examples/data.txt")\n}\n')
_dv = run(SBX + ["verify-release", "--pkg", _rel])
expect(_dv.returncode == 1 and "drift" in _dv.stderr.lower(),
       "content drift under the statement refuses verification")

# ---------------------------------------------------------------------------
print("=== 8. the demos ===")
# ---------------------------------------------------------------------------

_r = run(BOOT + [DEMO])
expect(_r.returncode == 0 and "digest 893425381" in _r.stdout,
       "the demo runs under the interpreter")
_r_ok = run(BOOT + [OK_TEST])
expect(_r_ok.returncode == 0 and "8 checks" in _r_ok.stdout,
       "the ok-test runs under the interpreter")

if _have_cc and os.path.isfile(os.path.join(TMP, "sandbox_demo.c")):
    _nb = os.path.join(BIN, "sandbox_demo")
    _a, _b2 = run(BOOT + [DEMO]).stdout, run([_nb]).stdout
    expect(_a == _b2,
           "interpreter and native agree byte for byte (the demo)")
    _okb = os.path.join(BIN, "ok_test")
    run(BOOT + [HLC, OK_TEST, os.path.join(TMP, "ok_test.c")])
    run(["cc", "-O2", "-o", _okb, os.path.join(TMP, "ok_test.c"),
         "-lm", "-pthread"])
    _a2, _b3 = run(BOOT + [OK_TEST]).stdout, run([_okb]).stdout
    expect(_a2 == _b3,
           "interpreter and native agree byte for byte (the ok-test)")
    _pd = run(BOOT + [os.path.join(PKG_DEMO, "main.hls")])
    expect(_pd.returncode == 0 and "digest 893425381" in _pd.stdout,
           "the package demo runs under the interpreter")
else:
    skip("native parity needs cc (the boot half was covered in "
         "section 5)")

# ---------------------------------------------------------------------------
print("=== 9. the tools ===")
# ---------------------------------------------------------------------------

for f in (DEMO, OK_TEST,
          os.path.join(PKG_DEMO, "main.hls"),
          os.path.join(PKG_DEMO, "libs", "sandbox_lib", "main.hls")):
    p1f = os.path.join(TMP, "fmt1.hls")
    p2f = os.path.join(TMP, "fmt2.hls")
    shutil.copyfile(f, p1f)
    run([sys.executable, "tools/hlfmt.py", "-w", p1f])
    shutil.copyfile(p1f, p2f)
    run([sys.executable, "tools/hlfmt.py", "-w", p2f])
    once = open(p1f).read()
    twice = open(p2f).read()
    expect(once == twice, "hlfmt: stable formatting on %s" % f)

lints = [run([sys.executable, "tools/hllint.py", f])
         for f in (DEMO, OK_TEST)]
expect(all(l.returncode == 0 for l in lints),
       "hllint: no findings on the new sources")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 110 gate: %d passed / %d failed / %d skipped"
      % (PASS, FAIL, SKIP))
sys.exit(1 if FAIL else 0)
