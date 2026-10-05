#!/usr/bin/env python3
"""hls-sandbox — sandboxed execution, seccomp-bpf (Stage 110).

Usage:
  python3 tools/hls-sandbox.py profile <entry.hls> [flags]
  python3 tools/hls-sandbox.py profile --pkg DIR [flags]
  python3 tools/hls-sandbox.py run [--pkg DIR | --effects IO,Fs | <entry.hls>]
                                    [flags] -- CMD [ARGS...]
  python3 tools/hls-sandbox.py emit [--pkg DIR | --effects ... | <entry.hls>]
                                    [--arch x86_64|aarch64|riscv64] [--out FILE]
  python3 tools/hls-sandbox.py release --pkg DIR [--out DIR]
  python3 tools/hls-sandbox.py verify-release --pkg DIR
  python3 tools/hls-sandbox.py selftest

The effect system says what a program MAY do; the audit says what
the supply chain DECLARES; this tool makes the kernel hold the
line. A profile is DERIVED (the checker's own computed surface —
source mode; the root package's audited surface — package mode) or
STATED (--effects, the ad-hoc spelling), compiled to a seccomp-bpf
filter, and then either installed by the launcher around a command
(`run`) or emitted as a C shim a binary compiles into itself
(`emit`).

The Fs read/write split: a tree whose reachable builtin census is
read-only arms `open`/`openat` under a write-intent argument mask —
the kernel refuses to open for writing. One write-side builtin or
one opaque `extern` declaring Fs, and the derived mode is honestly
rw. `--fs ro` tightens; `--fs rw` widens (the report says so).

Package mode is fail-closed, in the family's order: drift, then
unauditable, then the [effects] policy gate — a tree that fails
its own audit does not get armed (the audit is the gate; the
sandbox is the enforcement). --effects overrides the GATE's
yardstick (the hls-audit --allow spelling); --no-gate skips it
for local triage.

Flags:
  --fs ro|rw            the fs mode (default: derived)
  --default errno|kill|trap|log   the denial posture (default errno)
  --strict              --default kill, spelled the way people say it
  --baseline auto|minimal|hosted  minimal = a static artifact's set;
                        hosted adds the dynamic loader/libc prologue;
                        auto reads the target ELF (run mode)
  --allow-sysc A,B      extra syscalls by name (the CLI spelling of
                        [sandbox] allow)
  --deny-sysc A,B       carve-outs by name (exit/exit_group/brk are
                        not deniable; execve denial needs the shim)
  --json                machine-readable report (profile / run)
  --bpf                 profile: also print the filter listing
  --no-gate             package mode: skip the audit gate

Exit contract: 0 success (for `run`: the artifact's own code is
passed through); 1 policy/audit/release failures and selftest
failures; 2 usage errors. The run contract extends the shell's:
125 the sandbox could not be armed, 126 cannot execute, 127 not
found — and SIGSYS (observed as 159 = 128+31) is the kill-mode
denial.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOL_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from sandbox_parts import sbx_bpf as bpf                      # noqa: E402
from sandbox_parts import sbx_cemit                            # noqa: E402
from sandbox_parts import sbx_release as rel                   # noqa: E402
from sandbox_parts import sbx_exec                             # noqa: E402
from sandbox_parts.sbx_policy import (                         # noqa: E402
    ALL_EFFECTS, BASELINES, DEFAULT_ACTIONS, Profile, PolicyError,
    arch_of_host, derive_source, is_static_elf, nr_of,
    parse_sandbox_section, _SYSCALLS)

VERSION = "0.129.0-alpha"
TOOL = "hls-sandbox"


class Usage(Exception):
    """Exit 2."""


def _eprint(*a):
    sys.stderr.write("%s: %s\n" % (TOOL, " ".join(str(x) for x in a)))


# ---------------------------------------------------------------------------
# Profile construction (the three modes share this).
# ---------------------------------------------------------------------------

def _parse_effects(raw):
    if raw is None:
        return None
    out = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        if x not in ALL_EFFECTS:
            raise PolicyError("unknown effect: %s (the audit's "
                              "vocabulary: %s)" % (x, ", ".join(ALL_EFFECTS)))
        out.append(x)
    return out or None


def _parse_sysc(raw, what):
    if raw is None:
        return []
    out = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        if x not in _SYSCALLS:
            raise PolicyError("unknown syscall in --%s: %s (known: %s)"
                              % (what, x, ", ".join(sorted(_SYSCALLS))))
        out.append(x)
    return out


def _baseline_choice(requested, target):
    if requested != "auto":
        return requested
    if target and os.path.isfile(target):
        return "minimal" if is_static_elf(target) else "hosted"
    return "hosted"


def build_profile(args, target=None):
    """Resolve the mode, derive or state the surface, apply the
    overrides, return (profile, context) where context feeds the
    report."""
    default_action = ("kill" if args.strict
                      else (args.default or "errno"))
    allow = _parse_sysc(args.allow_sysc, "allow-sysc")
    deny = _parse_sysc(args.deny_sysc, "deny-sysc")
    baseline = _baseline_choice(args.baseline, target)
    fs = args.fs

    if getattr(args, "pkg", None):
        # -- package mode: gate, then derive the root entry -------
        pkg_dir = os.path.realpath(os.path.abspath(args.pkg))
        manifest_path = os.path.join(pkg_dir, "hls-pkg.toml")
        if not os.path.isfile(manifest_path):
            raise PolicyError("no hls-pkg.toml in %s" % pkg_dir)
        from hpkg_manifest import parse_manifest
        manifest = parse_manifest(manifest_path)
        sbx = parse_sandbox_section(manifest)

        gate = None
        if not args.no_gate:
            allow_eff = _parse_effects(args.effects)
            r = sbx_policy_mod().hls_audit.audit_package(pkg_dir, None)
            gate = {"drift": r["drift"], "unauditable": r["unauditable"],
                    "violations": r["violations"], "lockfile": r["lockfile"]}
            if r["drift"]:
                names = ", ".join(sorted({d["package"]
                                          for d in r["drift"]}))
                raise PolicyError(
                    "drift between the lockfile and the tree (%s) — "
                    "re-lock; the sandbox arms named content" % names)
            if r["unauditable"]:
                names = ", ".join(sorted({u["package"]
                                          for u in r["unauditable"]}))
                raise PolicyError(
                    "unauditable package(s) in the tree (%s) — fail "
                    "closed" % names)
            if allow_eff is not None:
                gate["yardstick"] = "--effects"
                allowed = set(allow_eff)
            else:
                eff_sec = (r.get("policy") or {}).get("allowed")
                allowed = set(eff_sec) if eff_sec is not None else None
                gate["yardstick"] = ("manifest [effects].allowed"
                                     if allowed is not None else "(none)")
            if allowed is not None:
                bad = [v for v in r["violations"]
                       if v["effect"] not in allowed]
                if bad:
                    raise PolicyError(
                        "the tree violates its own policy: %s — the "
                        "audit gate failed; a sandbox must not paper "
                        "over it" % ", ".join(
                            "%s via %s" % (v["effect"], v["package"])
                            for v in bad))

        entry = rel._resolve_entry(pkg_dir, manifest)
        deriv = derive_source(entry)
        fs_mode = fs or sbx["fs"] or deriv["fs_mode"]
        if fs:
            fs_origin = "--fs"
        elif sbx["fs"] and sbx["fs"] != deriv["fs_mode"]:
            fs_origin = ("manifest [sandbox] fs (tightens the derived "
                         "%s)" % (deriv["fs_mode"] or "none"))
        else:
            fs_origin = deriv["fs_origin"]
        da = sbx["default"] or default_action
        profile = Profile(deriv["effects"], fs_mode, fs_origin, da,
                          baseline,
                          extra_allow=allow or sbx["allow"],
                          deny=deny or sbx["deny"])
        return profile, {"mode": "package", "root": pkg_dir,
                         "derivation": deriv, "gate": gate}

    if args.entry:
        # -- source mode: the checker is the oracle ----------------
        if args.effects is not None:
            raise PolicyError(
                "--effects is the ad-hoc spelling — an entry file's "
                "surface is its checker's answer, not a claim; drop "
                "the file or drop the flag")
        deriv = derive_source(args.entry)
        fs_mode = fs or deriv["fs_mode"]
        fs_origin = "--fs" if fs else deriv["fs_origin"]
        profile = Profile(deriv["effects"], fs_mode, fs_origin,
                          default_action, baseline,
                          extra_allow=allow, deny=deny)
        return profile, {"mode": "source", "root": args.entry,
                         "derivation": deriv}

    if args.effects is not None:
        # -- ad-hoc: the operator states the surface ---------------
        effects = _parse_effects(args.effects)
        if fs is None and "Fs" in effects:
            fs = "rw"    # no census to lean on — the honest floor
        fs_origin = ("--fs" if fs else "no Fs in the stated surface")
        profile = Profile(effects or [], fs, fs_origin, default_action,
                          baseline, extra_allow=allow, deny=deny)
        return profile, {"mode": "adhoc", "root": None}

    raise Usage("nothing to derive or state: give an entry file, "
                "--pkg DIR, or --effects E1,E2")


def sbx_policy_mod():
    from sandbox_parts import sbx_policy as P
    return P


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------

def render_profile(rep, with_bpf=False, program=None, names=None):
    out = []
    out.append("hls-sandbox — seccomp-bpf profile (%s mode)" % rep["mode"])
    if rep.get("root"):
        out.append("root: %s" % rep["root"])
    d = rep.get("derivation")
    if d:
        out.append("execution surface (checker%s): %s"
                   % (", from main" if d["scope"] == "main"
                      else " — " + d["scope"],
                      ", ".join(d["effects"]) or "(none)"))
        if d.get("builtins"):
            out.append("reachable builtins: %s" % ", ".join(d["builtins"]))
        if d.get("extern_effects"):
            out.append("extern-declared effects: %s"
                       % ", ".join(d["extern_effects"]))
    out.append("fs mode: %s (%s)"
               % (rep["fs"]["mode"] or "(none)", rep["fs"]["origin"]))
    out.append("default action: %s — %s"
               % (rep["default_action"],
                  DEFAULT_ACTIONS[rep["default_action"]]))
    out.append("baseline: %s" % rep["baseline"])
    for eff in rep["effects"]:
        out.append("layer %s: %s" % (eff, ", ".join(rep["layers"][eff])
                                     or "(empty — %s carries no syscalls "
                                        "of its own)" % eff))
    if rep.get("gate") is not None:
        g = rep["gate"]
        out.append("gate: %s (drift %d, unauditable %d, violations %d)"
                   % (g.get("yardstick", "?"), len(g["drift"]),
                      len(g["unauditable"]), len(g["violations"])))
    if rep.get("denied"):
        out.append("denied: %s" % ", ".join(rep["denied"]))
    if rep.get("extra_allow"):
        out.append("extra allow: %s" % ", ".join(rep["extra_allow"]))
    out.append("allowlist (%s): %d syscalls, %d BPF instructions"
               % (rep["arch"], rep["allowlist"]["count"],
                  rep["program"]["instructions"]))
    if rep["allowlist"]["constrained"]:
        for name, cons in sorted(
                rep["allowlist"]["constrained"].items()):
            out.append("constrained: %s%s — the write-intent mask"
                       % (name, cons))
    for note in rep.get("absent_notes", []):
        out.append("note: %s" % note)
    if with_bpf and program is not None:
        out.append("")
        out.append("filter listing:")
        out.append(bpf.disassemble(program, names or {}))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_profile(args):
    profile, ctx = build_profile(args)
    arch = args.arch or arch_of_host()
    if arch is None:
        raise PolicyError("this machine is none of x86_64 / aarch64 / "
                          "riscv64 — pass --arch explicitly")
    rep = profile.to_report(arch, ctx["mode"], ctx.get("root"),
                            derivation=ctx.get("derivation"),
                            gate=ctx.get("gate"))
    if args.json:
        print(json.dumps(rep, sort_keys=True, indent=2))
    else:
        rules = profile.rules(arch)
        names = {rules[nr].nr: rules[nr].name for nr in rules}
        print(render_profile(rep, with_bpf=args.bpf,
                             program=profile.program(arch), names=names))
    return 0


def cmd_run(args):
    cmd = list(getattr(args, "cmd", None) or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise Usage("run needs a command after -- "
                    "(hls-sandbox.py run [flags] -- CMD ARGS)")
    if not os.path.isfile(cmd[0]) and os.sep not in cmd[0]:
        # let the PATH search happen in the child; but the profile's
        # baseline auto-detection needs a real path when it can get one
        target = None
    else:
        target = cmd[0]
    profile, ctx = build_profile(args, target=target)
    arch = arch_of_host()
    if arch is None:
        raise PolicyError("this machine is none of x86_64 / aarch64 / "
                          "riscv64 — the launcher refuses to install "
                          "numbers it cannot vouch for here")
    ok, why = sbx_exec.seccomp_supported(arch)
    if not ok:
        raise PolicyError("cannot arm on this host: %s" % why)
    code, sig, err = sbx_exec.run(profile, arch, cmd)
    rep = {
        "schema": "hls-sandbox-run/v1",
        "mode": ctx["mode"],
        "arch": arch,
        "effects": profile.effects,
        "fs": profile.fs_mode,
        "default_action": profile.default_action,
        "baseline": profile.baseline,
        "command": cmd,
        "exit": code,
        "signal": sig,
        "verdict": sbx_exec.describe_exit(code, sig),
    }
    if args.json:
        print(json.dumps(rep, sort_keys=True, indent=2))
    elif not args.quiet:
        sys.stderr.write("hls-sandbox: %s\n" % rep["verdict"])
    return code


def cmd_emit(args):
    profile, ctx = build_profile(args)
    arch = args.arch
    if arch is None:
        raise Usage("emit needs --arch (the shim is cross-arch by "
                    "design — name the target)")
    out = args.out or ("hls_sandbox_%s.c"
                       % ("_".join(e.lower() for e in profile.effects)
                          or "bare"))
    path = sbx_cemit.emit_shim(profile, arch, out)
    rules = profile.rules(arch)
    print("%s: %s — %d syscalls, arch %s, default %s"
          % (TOOL, path, len(rules), arch, profile.default_action))
    return 0


def cmd_release(args):
    if not args.pkg:
        raise Usage("release needs --pkg DIR")
    statement, st_path, rec = rel.release(args.pkg, out_dir=args.out)
    print("%s: %s" % (TOOL, st_path))
    print("  effects: %s" % ", ".join(statement["effects"]))
    print("  fs: %s, default: %s, baseline: %s"
          % (statement["fs"]["mode"], statement["default_action"],
             statement["baseline"]))
    print("  allowlist: %d syscalls (%d constrained)"
          % (len(statement["allowlist"]), len(statement["constrained"])))
    print("  ledger: seq %s, kind sandbox, chain %s..."
          % (rec.get("seq"), str(rec.get("chain_hash"))[:12]))
    return 0


def cmd_verify_release(args):
    if not args.pkg:
        raise Usage("verify-release needs --pkg DIR")
    statement, checks = rel.verify_release(args.pkg)
    print("%s: %s %s — VERIFIED" % (TOOL, statement["name"],
                                    statement["version"]))
    for c in checks:
        print("  %s" % c)
    return 0


def cmd_selftest(args):
    return selftest()


# ---------------------------------------------------------------------------
# The selftest — the stage's pinned numbers, through the front door.
# ---------------------------------------------------------------------------

def selftest():
    from sandbox_parts import sbx_policy as P
    from boot.checking.helpers import BUILTIN_EFFECTS

    total = [0, 0]

    def check(cond, msg):
        total[0] += 1
        if cond:
            print("  [PASS] %s" % msg)
        else:
            total[1] += 1
            print("  [FAIL] %s" % msg)

    print("hls-sandbox selftest — %s" % VERSION)

    # 1. the one vocabulary
    check(P.ALL_EFFECTS is not None
          and sorted(P.ALL_EFFECTS) == sorted(ALL_EFFECTS),
          "the effect vocabulary is hls-audit's (one set)")
    for b in P.FS_READ_BUILTINS + P.FS_WRITE_BUILTINS:
        check(BUILTIN_EFFECTS.get(b) == {"Fs"},
              "fs census: %s is an Fs builtin in the one true table" % b)
    check(not (set(P.FS_READ_BUILTINS) & set(P.FS_WRITE_BUILTINS)),
          "fs census: the read and write sides do not overlap")

    # 2. the arch tables
    check(all(row[1] == row[2] for row in _SYSCALLS.values()),
          "aarch64 and riscv64 share the asm-generic numbering (every row)")
    check(nr_of("stat", "x86_64")[0] == 4
          and nr_of("stat", "aarch64")[0] is None,
          "plain stat exists on x86_64 only, with the recorded reason")
    check(nr_of("openat", "riscv64")[0] == 56,
          "openat is 56 on the asm-generic arches")
    check(nr_of("seccomp", "x86_64")[0] == 317,
          "seccomp itself is 317 on x86_64")

    # 3. the layer invariant (strict) — every unmarked name on all arches
    missing = []
    for eff, entries in P.LAYERS.items():
        for entry in entries:
            name = entry[0] if isinstance(entry, tuple) else entry
            for arch in ("x86_64", "aarch64", "riscv64"):
                nr, note = nr_of(name, arch)
                if nr is None and note is None:
                    missing.append((eff, name, arch))
    check(not missing, "every layer syscall resolves (or is absent with "
                       "a reason) on all three arches")

    # 4. the flags-argument contract — the regression the kernel
    #    taught us: open's flags are arg1, openat's are arg2.
    check(P.OPEN_FLAGS_ARG == {"open": 1, "openat": 2},
          "the write-intent mask reads open's arg1 and openat's arg2")
    ro = P.Profile(["Fs"], "ro", "selftest", "errno", "minimal")
    rules = ro.rules("x86_64")
    o = [rules[nr] for nr in rules if rules[nr].name == "openat"][0]
    check(sorted(o.constraints) == [(2, P.OPEN_WRITE_MASK)],
          "Fs(ro) masks openat's flags (arg2) with the write-intent mask")
    check(o.unconditional is False,
          "Fs(ro)'s openat is conditional — the mask is the rule")

    # 5. merge semantics: rw absorbs the ro justification
    rw = P.Profile(["Fs"], "rw", "selftest", "errno", "minimal")
    rules = rw.rules("x86_64")
    o = [rules[nr] for nr in rules if rules[nr].name == "openat"][0]
    check(o.unconditional and not o.constraints,
          "Fs(rw)'s openat is unconditional (the rw justification "
          "absorbs every read-only one)")

    # 6. pinned program shapes
    pure = P.Profile([], None, "selftest", "errno", "minimal")
    prog = pure.program("x86_64")
    check(len(prog) == 29,
          "the pure minimal program is 29 instructions (pinned)")
    check(prog.instrs[0].words() == b"\x20\x00\x00\x00\x04\x00\x00\x00"
          and prog.instrs[3].words() == b"\x20\x00\x00\x00\x00\x00\x00\x00",
          "the program opens arch-load, arch-guard, deny, nr-load")
    check(prog.instrs[1].k == bpf.AUDIT_ARCH_X86_64,
          "the arch guard pins AUDIT_ARCH_X86_64 on x86_64")
    fr = P.Profile(["IO", "Fs"], "ro", "selftest", "errno", "hosted")
    check(len(fr.program("x86_64")) == 93,
          "the IO+Fs(ro) hosted program is 93 instructions (pinned)")
    fw = P.Profile(["IO", "Fs"], "rw", "selftest", "errno", "hosted")
    check(len(fw.program("x86_64")) == 109,
          "the IO+Fs(rw) hosted program is 109 instructions (pinned)")
    check(len(fr.rules("x86_64")) == 41 and len(fw.rules("x86_64")) == 52,
          "ro allows 41 syscalls, rw 52 (pinned)")

    # 7. determinism
    check(fr.program("x86_64") == fr.program("x86_64")
          and fr.program("x86_64").bytes()
          == P.Profile(["IO", "Fs"], "ro", "selftest", "errno",
                       "hosted").program("x86_64").bytes(),
          "assembly is deterministic (same rules, same bytes)")

    # 8. the disassembler round-trips the arch guard
    txt = bpf.disassemble(prog)
    check("ld     arch" in txt.split("\n")[0]
          and "jeq" in txt.split("\n")[1],
          "the disassembler renders the fixed shape")

    # 9. refusals
    try:
        P.Profile(["Bogus"], None, "selftest", "errno", "hosted")
        check(False, "an unknown effect is refused")
    except PolicyError:
        check(True, "an unknown effect is refused")
    try:
        P.Profile(["IO"], None, "selftest", "vaporize", "hosted")
        check(False, "an unknown default action is refused")
    except PolicyError:
        check(True, "an unknown default action is refused")
    try:
        P.Profile(["IO"], None, "selftest", "errno", "hosted",
                   deny=["exit_group"])
        check(False, "deny on exit_group is refused (the hang rule)")
    except PolicyError:
        check(True, "deny on exit_group is refused (the hang rule)")
    try:
        P.Profile(["IO"], None, "selftest", "errno", "hosted",
                   deny=["execve"])
        check(True, "deny on execve builds (the shim's profile)")
    except PolicyError:
        check(False, "deny on execve builds (the shim's profile)")

    # 10. the jump-range refusal — the assembler's own guard, driven
    #     directly (a profile that large cannot be built from the
    #     current tables, and the guard is the mechanism, so the
    #     vector goes to the mechanism).
    big = bpf.Asm()
    big.load(bpf.OFF_NR)
    for i in range(300):
        big.jeq(i, jt_label="far%d" % i)
    big.ret(bpf.SECCOMP_RET_ALLOW)
    for i in range(300):
        big.label("far%d" % i)
        big.ret(bpf.SECCOMP_RET_ALLOW)
    try:
        big.finish()
        check(False, "an over-range jump is refused, not truncated")
    except Exception as ex:
        check("8-bit" in str(ex) or "too large" in str(ex),
              "an over-range jump is refused, not truncated (%s)"
              % str(ex)[:40])
    okasm = bpf.Asm()
    okasm.load(bpf.OFF_NR)
    okasm.jeq(1, jt_label="one")
    okasm.ret(bpf.SECCOMP_RET_ERRNO | 1)
    okasm.label("one")
    okasm.ret(bpf.SECCOMP_RET_ALLOW)
    small = okasm.finish()
    check(small.instrs[1].jt == 1 and small.instrs[1].jf == 0,
          "a resolved jump is relative to the NEXT instruction "
          "(jeq -> +1 lands two down)")

    # 11. the manifest section
    try:
        P.parse_sandbox_section({"sandbox": {"default": "teleport"}})
        check(False, "[sandbox] default is validated")
    except PolicyError:
        check(True, "[sandbox] default is validated")
    try:
        P.parse_sandbox_section({"sandbox": {"allow": ["not_a_syscall"]}})
        check(False, "[sandbox] allow names are validated")
    except PolicyError:
        check(True, "[sandbox] allow names are validated")
    ok_sec = P.parse_sandbox_section(
        {"sandbox": {"fs": "ro", "deny": ["ptrace"] if "ptrace"
                     in _SYSCALLS else ["kill"]}})
    check(ok_sec["fs"] == "ro" and len(ok_sec["deny"]) == 1,
          "[sandbox] parses the tightening knobs")

    # 12. the ELF static probe
    check(P.is_static_elf("/bin/sh") is False,
          "the ELF probe reads /bin/sh as dynamic")

    print("hls-sandbox selftest: %d passed / %d failed"
          % (total[0], total[1]))
    return 0 if total[1] == 0 else 1


# ---------------------------------------------------------------------------
# The CLI.
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # `run CMD -- args...`: split at the FIRST `--` so the command's
    # own flags are never parsed as ours (argparse's REMAINDER would
    # swallow everything after the first positional, including
    # --strict placed after the entry file).
    cmd_tail = None
    if "--" in argv:
        i = argv.index("--")
        cmd_tail = argv[i + 1:]
        argv = argv[:i]

    ap = argparse.ArgumentParser(
        prog=TOOL, add_help=True,
        description="hls-sandbox — sandboxed execution (seccomp-bpf)")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, entry=True):
        if entry:
            p.add_argument("entry", nargs="?", default=None,
                           help="the entry .hls (source mode)")
        p.add_argument("--pkg", default=None,
                       help="package mode: the package directory")
        p.add_argument("--effects", default=None,
                       help="ad-hoc surface / the gate's yardstick "
                            "(E1,E2,...)")
        p.add_argument("--fs", default=None, choices=["ro", "rw"])
        p.add_argument("--default", default=None,
                       choices=sorted(DEFAULT_ACTIONS))
        p.add_argument("--strict", action="store_true",
                       help="--default kill")
        p.add_argument("--baseline", default="auto", choices=BASELINES)
        p.add_argument("--allow-sysc", default=None)
        p.add_argument("--deny-sysc", default=None)
        p.add_argument("--no-gate", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("profile", help="derive and print the profile")
    common(p)
    p.add_argument("--bpf", action="store_true",
                   help="also print the filter listing")
    p.add_argument("--arch", default=None,
                   choices=["x86_64", "aarch64", "riscv64"])
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser("run", help="run a command under the filter")
    common(p)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_run, cmd_tail=None)

    p = sub.add_parser("emit", help="emit the self-arming C shim")
    common(p)
    p.add_argument("--arch", default=None,
                   choices=["x86_64", "aarch64", "riscv64"],
                   required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_emit)

    p = sub.add_parser("release", help="write the statement + chain it")
    p.add_argument("--pkg", required=True)
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_release)

    p = sub.add_parser("verify-release", help="verify a statement")
    p.add_argument("--pkg", required=True)
    p.set_defaults(fn=cmd_verify_release)

    p = sub.add_parser("selftest", help="the stage's pinned numbers")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None) or not hasattr(args, "fn"):
        ap.print_help()
        return 2
    if cmd_tail is not None:
        args.cmd = cmd_tail
    try:
        return args.fn(args)
    except Usage as ex:
        _eprint(str(ex))
        return 2
    except (PolicyError, rel.ReleaseError) as ex:
        _eprint(str(ex))
        return 1
    except sbx_policy_mod().hls_audit.AuditError as ex:
        _eprint(str(ex))
        return 1


if __name__ == "__main__":
    sys.exit(main())
