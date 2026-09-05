#!/usr/bin/env python3
"""hlcross.py — Stage 22 (v0.41.0-alpha): cross-compilation orchestrator.

Drives the full cross-compilation pipeline:

  HLS source  --hlc-->  C source  --cross-linker-->  foreign binary

The C backend of `hlc` is portable ANSI C11: the generated `*.c` file
can be compiled by ANY C compiler that targets the destination. The
cross-compilation problem reduces to picking the right cross-linker
and the right target-specific flags.

Supported targets (the roadmap's Stage 22 set):

  x86_64-linux-gnu           Linux x86-64 (glibc, ELF)
  x86_64-unknown-freebsd     FreeBSD x86-64 (ELF)
  aarch64-apple-darwin       macOS Apple Silicon (Mach-O)
  x86_64-pc-windows-msvc     Windows x86-64 (PE COFF, MSVC ABI)
  x86_64-pc-windows-gnu      Windows x86-64 (PE COFF, MinGW ABI)

Cross-linker detection order (the FIRST available wins):

  1. `zig cc -target <triple>`  — the universal linker. When zig is
     installed, EVERY target works through a single toolchain.
  2. Target-specific cross-linkers:
     - x86_64-pc-windows-gnu    -> x86_64-w64-mingw32-gcc
     - aarch64-linux-gnu        -> aarch64-linux-gnu-gcc
     - x86_64-unknown-freebsd   -> x86_64-unknown-freebsd13-gcc
     - aarch64-apple-darwin     -> aarch64-apple-darwin-clang (osxcross)
  3. The host compiler (gcc/clang) when the target triple matches the
     host (a NATIVE build — useful for testing the pipeline end-to-end
     without a real cross-linker).

When no cross-linker is available, `hlcross` reports SKIP with a
clear message about which toolchain to install. The HLS -> C step
ALWAYS succeeds (the C file is written even when linking fails) —
useful for shipping the C source to a target machine for compilation
there.

Usage:
  python3 tools/hlcross.py <input.hls> <output.bin> [--target <triple>]
                                                  [--linker zig|gcc|clang|auto]
                                                  [--keep-c <path>]
                                                  [--dry-run]
                                                  [--list-targets]
                                                  [--show-host]
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Target registry — the canonical Stage 22 target set + their properties.
# ---------------------------------------------------------------------------

TARGETS = {
    "x86_64-linux-gnu": {
        "arch": "x86_64",
        "os": "linux",
        "abi": "gnu",
        "binary_format": "ELF x86-64",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm", "-lpthread"],
        "mingw": False,
        # Stage 25 (v0.44.0-alpha): security hardening flags applied
        # when --security pac+bti (or auto on aarch64-darwin/graviton).
        # Empty for x86-64 (PAC/BTI are ARM-specific).
        "security_flags": [],
    },
    "x86_64-unknown-freebsd": {
        "arch": "x86_64",
        "os": "freebsd",
        "abi": "freebsd",
        "binary_format": "ELF x86-64 (FreeBSD)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm", "-lpthread"],
        "mingw": False,
        "security_flags": [],
    },
    "aarch64-apple-darwin": {
        "arch": "arm64",
        "os": "macos",
        "abi": "darwin",
        "binary_format": "Mach-O arm64",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm"],
        "mingw": False,
        # Stage 25: Apple Silicon supports PAC (Pointer Authentication)
        # and BTI (Branch Target Identification). The default
        # -mbranch-protection leaves the compiler's default; passing
        # pac-ret+bti enables both. Applied only when --security pac+bti
        # is given (default: auto, which enables on Apple Silicon when
        # the cross-linker is zig cc).
        "security_flags": ["-mbranch-protection=pac-ret+bti"],
    },
    # Stage 25 (v0.44.0-alpha): AArch64 Linux targets (Graviton 3+,
    # Raspberry Pi 4, etc.). The C backend is portable ANSI C11; the
    # cross-compilation reduces to picking the right cross-linker
    # (zig cc, aarch64-linux-gnu-gcc) and the right security flags
    # (BTI on Graviton 3+; PAC + BTI on Apple Silicon emulation).
    "aarch64-linux-gnu": {
        "arch": "arm64",
        "os": "linux",
        "abi": "gnu",
        "binary_format": "ELF aarch64 (Little Endian)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm", "-lpthread"],
        "mingw": False,
        # Stage 25: Graviton 3+ supports BTI (Branch Target
        # Identification). PAC is also supported on Graviton 4. The
        # default -mbranch-protection=bti enables just BTI; pass
        # pac-ret+bti for full PAC+BTI (Apple Silicon + Graviton 4).
        "security_flags": ["-mbranch-protection=bti"],
    },
    "aarch64-unknown-linux-gnu": {
        "arch": "arm64",
        "os": "linux",
        "abi": "gnu",
        "binary_format": "ELF aarch64 (Little Endian)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm", "-lpthread"],
        "mingw": False,
        "security_flags": ["-mbranch-protection=bti"],
    },
    "x86_64-pc-windows-msvc": {
        "arch": "x86_64",
        "os": "windows",
        "abi": "msvc",
        "binary_format": "PE COFF x86-64 (MSVC ABI)",
        "object_suffix": ".obj",
        "binary_suffix": ".exe",
        "link_libs": [],
        "mingw": False,
        "security_flags": [],
    },
    "x86_64-pc-windows-gnu": {
        "arch": "x86_64",
        "os": "windows",
        "abi": "gnu",
        "binary_format": "PE COFF x86-64 (MinGW ABI)",
        "object_suffix": ".o",
        "binary_suffix": ".exe",
        "link_libs": ["-lm"],
        "mingw": True,
        "security_flags": [],
    },
}

# Aliases — accept the short forms users commonly type.
TARGET_ALIASES = {
    "linux": "x86_64-linux-gnu",
    "linux64": "x86_64-linux-gnu",
    "freebsd": "x86_64-unknown-freebsd",
    "macos": "aarch64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
    "darwin": "aarch64-apple-darwin",
    "windows": "x86_64-pc-windows-gnu",
    "windows-msvc": "x86_64-pc-windows-msvc",
    "windows-gnu": "x86_64-pc-windows-gnu",
    # Stage 25 (v0.44.0-alpha): AArch64 Linux aliases.
    "aarch64-linux": "aarch64-linux-gnu",
    "aarch64": "aarch64-linux-gnu",
    "arm64": "aarch64-linux-gnu",
    "arm64-linux": "aarch64-linux-gnu",
    "graviton": "aarch64-linux-gnu",
    "rpi4": "aarch64-linux-gnu",
    "raspberrypi": "aarch64-linux-gnu",
}


def canonical_target(name: str) -> str:
    """Resolve a target alias to its canonical triple."""
    if name in TARGETS:
        return name
    if name in TARGET_ALIASES:
        return TARGET_ALIASES[name]
    raise ValueError(f"unknown target '{name}'. Use --list-targets to see "
                     f"the supported set.")


# ---------------------------------------------------------------------------
# Host detection — for the "native build" fallback path.
# ---------------------------------------------------------------------------

def host_triple() -> str:
    """Return the canonical triple of the host we're running on."""
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-linux-gnu"
        if machine in ("aarch64", "arm64"):
            return "aarch64-linux-gnu"  # not in the Stage 22 set but useful
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        if machine in ("x86_64", "amd64"):
            return "x86_64-apple-darwin"
    if system.startswith("freebsd"):
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-freebsd"
    if system == "windows":
        return "x86_64-pc-windows-gnu"
    return ""


# ---------------------------------------------------------------------------
# Cross-linker detection.
# ---------------------------------------------------------------------------

def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def find_zig() -> Optional[str]:
    """Return the path to `zig` if available, else None."""
    return _which("zig")


def find_target_linker(target: str) -> Tuple[Optional[str], List[str], str]:
    """Find a cross-linker for `target`.

    Returns (linker_path, base_args, kind) where:
      - linker_path: the executable to invoke (or None if not found)
      - base_args: the args to pass BEFORE the input/output paths
      - kind: a human-readable description of the linker strategy
              ("zig", "mingw", "osxcross", "freebsd-gcc", "native",
              "host-fallback", "not-found")

    Detection order:
      1. zig cc -target <triple>  (universal)
      2. target-specific cross-linker
      3. host compiler when target == host (native build)
    """
    spec = TARGETS[target]
    # 1. zig cc — the universal linker.
    zig = find_zig()
    if zig:
        # zig cc -target <triple> behaves as a cross-compiler for every
        # target zig supports (Linux, macOS, Windows, FreeBSD, ...).
        return (zig, ["cc", "-target", target, "-O2"], "zig")

    # 2. target-specific cross-linkers.
    if target == "x86_64-pc-windows-gnu":
        p = _which("x86_64-w64-mingw32-gcc")
        if p:
            return (p, ["-O2"], "mingw")
    if target == "x86_64-pc-windows-msvc":
        # MSVC target — zig is the only practical cross-linker on Linux.
        # cl.exe is only available on Windows itself.
        pass
    if target == "aarch64-apple-darwin":
        # osxcross provides aarch64-apple-darwin-clang
        p = _which("aarch64-apple-darwin-clang")
        if p:
            return (p, ["-O2"], "osxcross")
    if target == "x86_64-unknown-freebsd":
        # FreeBSD cross-compiler naming varies; try the common ones.
        for name in ("x86_64-unknown-freebsd13-gcc",
                     "x86_64-unknown-freebsd14-gcc",
                     "x86_64-unknown-freebsd-gcc"):
            p = _which(name)
            if p:
                return (p, ["-O2"], "freebsd-gcc")
    # Stage 25 (v0.44.0-alpha): aarch64-linux-gnu cross-linkers.
    if target in ("aarch64-linux-gnu", "aarch64-unknown-linux-gnu"):
        # Try the Debian/Ubuntu-style cross-compiler name first.
        for name in ("aarch64-linux-gnu-gcc",
                     "aarch64-linux-gnu-gcc-12",
                     "aarch64-linux-gnu-gcc-11",
                     "aarch64-linux-gnu-cc"):
            p = _which(name)
            if p:
                return (p, ["-O2"], "aarch64-linux-gnu-gcc")

    # 3. host compiler when target == host (native build — useful for
    #    testing the pipeline end-to-end without a real cross-linker).
    host = host_triple()
    if target == host:
        for name in ("gcc", "clang", "cc"):
            p = _which(name)
            if p:
                return (p, ["-O2"], "native")

    # No cross-linker available.
    return (None, [], "not-found")


def cross_linker_hint(target: str, kind: str) -> str:
    """A human-readable hint about how to install the missing linker."""
    if kind == "not-found":
        return (f"no cross-linker found for target '{target}'. "
                f"Install `zig` (https://ziglang.org) for the universal "
                f"cross-linker, or a target-specific toolchain:\n"
                f"  - x86_64-pc-windows-gnu: apt install mingw-w64\n"
                f"  - aarch64-apple-darwin:  install osxcross\n"
                f"  - x86_64-unknown-freebsd: apt install freebsd-buildutils\n"
                f"  - x86_64-pc-windows-msvc: requires Windows + MSVC build tools")
    return ""


# ---------------------------------------------------------------------------
# Binary format inspection (validate the linker output).
# ---------------------------------------------------------------------------

def detect_binary_format(path: str) -> str:
    """Inspect a binary file and return its format (ELF/Mach-O/PE)."""
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
    except OSError:
        return "unreadable"
    # ELF: 0x7f 'E' 'L' 'F'
    if magic[:4] == b"\x7fELF":
        ei_class = magic[4]  # 1 = 32-bit, 2 = 64-bit
        ei_data = magic[5]   # 1 = LE, 2 = BE
        bits = "32-bit" if ei_class == 1 else "64-bit"
        endian = "LE" if ei_data == 1 else "BE"
        return f"ELF {bits} {endian}"
    # Mach-O: 0xFEEDFACE/0xFEEDFACF (32/64-bit) or reversed.
    if magic[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                     b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        if magic[:4] in (b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"):
            return "Mach-O 64-bit"
        return "Mach-O 32-bit"
    # PE/COFF: 'M' 'Z' header (DOS stub) at the start.
    if magic[:2] == b"MZ":
        return "PE COFF (Windows)"
    # COFF (no DOS stub): some MIPS/ARM targets start with 0x0000 + machine.
    return "unknown"


# ---------------------------------------------------------------------------
# The orchestrator.
# ---------------------------------------------------------------------------

def run(cmd: List[str], capture: bool = False) -> Tuple[int, str]:
    """Run a command; return (exit_code, output)."""
    if capture:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        return (r.returncode, r.stdout)
    r = subprocess.run(cmd)
    return (r.returncode, "")


def cross_compile(input_hls: str, output_bin: str, target: str,
                  linker_kind: str = "auto", keep_c: Optional[str] = None,
                  dry_run: bool = False, hlc: str = "bin/hlc",
                  security: str = "auto",
                  target_feature: str = "") -> int:
    """Cross-compile an HLS program to a foreign binary.

    Steps:
      1. hlc <input.hls> <tmp.c>          (HLS -> C, the portable backend)
      2. <cross-linker> <tmp.c> -o <out>  (C -> foreign binary)

    The C file is portable ANSI C11: any C compiler targeting the
    destination can compile it. The target triple is emitted as a
    comment in the C output (for traceability) but does not affect the
    C codegen (the runtime is target-independent).
    """
    if not os.path.exists(input_hls):
        print(f"error: input file not found: {input_hls}", file=sys.stderr)
        return 2
    target = canonical_target(target)
    spec = TARGETS[target]
    # Resolve the cross-linker.
    if linker_kind == "auto":
        linker, base_args, kind = find_target_linker(target)
    elif linker_kind == "zig":
        zig = find_zig()
        if not zig:
            print(f"error: --linker zig requested but zig is not installed",
                  file=sys.stderr)
            return 2
        linker, base_args, kind = zig, ["cc", "-target", target, "-O2"], "zig"
    elif linker_kind in ("gcc", "clang", "cc"):
        p = _which(linker_kind)
        if not p:
            print(f"error: --linker {linker_kind} not found", file=sys.stderr)
            return 2
        linker, base_args, kind = p, ["-O2"], "host-fallback"
    else:
        print(f"error: unknown --linker '{linker_kind}'", file=sys.stderr)
        return 2

    # Where to write the C file.
    if keep_c:
        c_path = keep_c
    else:
        c_path = output_bin + ".c"
    # Step 1: HLS -> C (always works; the C backend is target-agnostic).
    if dry_run:
        print(f"[dry-run] hlc {input_hls} {c_path}")
        print(f"[dry-run] linker: {linker or '(none)'} {base_args} {c_path} "
              f"-o {output_bin}")
        return 0
    print(f"[1/2] hlc: compiling {input_hls} -> {c_path}")
    # Stage 25 (v0.44.0-alpha): pass --target-feature through to hlc
    # when given (enables NEON/SSE/AVX intrinsic fast paths for std.simd).
    hlc_cmd = [hlc, input_hls, c_path]
    if target_feature:
        hlc_cmd.append("--target-feature")
        hlc_cmd.append(target_feature)
    code, _ = run(hlc_cmd)
    if code != 0:
        print(f"error: hlc failed (exit {code})", file=sys.stderr)
        return 1
    if not os.path.exists(c_path):
        print(f"error: hlc did not produce {c_path}", file=sys.stderr)
        return 1
    print(f"      C source: {c_path} ({os.path.getsize(c_path)} bytes)")
    # Step 2: C -> foreign binary (the cross-linker step).
    if linker is None:
        hint = cross_linker_hint(target, kind)
        print(f"[2/2] SKIP: {hint}", file=sys.stderr)
        print(f"      The C source at {c_path} can be copied to a "
              f"{spec['binary_format']} host and compiled there with "
              f"the platform's native cc.", file=sys.stderr)
        return 3  # 3 = SKIP (no cross-linker available)
    out_path = output_bin + spec["binary_suffix"]
    print(f"[2/2] {kind}: linking {c_path} -> {out_path}")
    # Stage 25 (v0.44.0-alpha): apply the target's security_flags when
    # --security auto (default) or --security pac+bti is given. The
    # flags are -mbranch-protection=... for AArch64 targets; empty for
    # x86-64 / Windows (PAC/BTI are ARM-specific).
    sec_flags = []
    if security == "auto":
        sec_flags = spec.get("security_flags", [])
    elif security == "pac+bti":
        sec_flags = ["-mbranch-protection=pac-ret+bti"]
    elif security == "bti":
        sec_flags = ["-mbranch-protection=bti"]
    elif security == "off":
        sec_flags = []
    cmd = [linker] + base_args + sec_flags + [c_path, "-o", out_path] + spec["link_libs"]
    code, _ = run(cmd)
    if code != 0:
        print(f"error: linker failed (exit {code})", file=sys.stderr)
        print(f"      command: {' '.join(cmd)}", file=sys.stderr)
        return 1
    fmt = detect_binary_format(out_path)
    print(f"      binary: {out_path} ({os.path.getsize(out_path)} bytes, "
          f"{fmt})")
    if not keep_c:
        try:
            os.unlink(c_path)
        except OSError:
            pass
    return 0


def cmd_list_targets() -> int:
    print("Supported cross-compilation targets (Stage 22):")
    print()
    for triple, spec in TARGETS.items():
        print(f"  {triple}")
        print(f"      arch: {spec['arch']}, os: {spec['os']}, abi: {spec['abi']}")
        print(f"      format: {spec['binary_format']}")
    print()
    print("Aliases (accepted by --target):")
    for alias, canonical in TARGET_ALIASES.items():
        print(f"  {alias:24s} -> {canonical}")
    print()
    host = host_triple()
    if host:
        print(f"Host triple detected: {host}")
    else:
        print("Host triple: (unknown)")
    return 0


def cmd_show_host() -> int:
    host = host_triple()
    if host:
        print(host)
        return 0
    print("(unknown)", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 22 cross-compilation orchestrator.")
    ap.add_argument("input", nargs="?", help="HLS source file")
    ap.add_argument("output", nargs="?", help="output binary path")
    ap.add_argument("--target", default="x86_64-linux-gnu",
                    help="target triple (use --list-targets to see options)")
    ap.add_argument("--linker", default="auto",
                    choices=["auto", "zig", "gcc", "clang", "cc"],
                    help="linker strategy (default: auto-detect)")
    ap.add_argument("--keep-c", help="keep the intermediate C file at PATH")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands without executing them")
    ap.add_argument("--list-targets", action="store_true",
                    help="list the supported target triples and exit")
    ap.add_argument("--show-host", action="store_true",
                    help="print the host's canonical triple and exit")
    ap.add_argument("--hlc", default="bin/hlc",
                    help="path to the native hlc compiler (default: bin/hlc)")
    # Stage 25 (v0.44.0-alpha): --security controls AArch64 PAC/BTI.
    ap.add_argument("--security", default="auto",
                    choices=["auto", "pac+bti", "bti", "off"],
                    help="Stage 25: AArch64 security hardening "
                         "(default: auto = use target's default; "
                         "pac+bti = full PAC + BTI; bti = BTI only; "
                         "off = no hardening)")
    # Stage 25: --target-feature neon passes through to hlc.
    ap.add_argument("--target-feature", default="",
                    choices=["", "neon", "sse4.2", "avx2", "native"],
                    help="Stage 25: enable std.simd intrinsic fast paths "
                         "(neon for AArch64; sse4.2/avx2 for x86; "
                         "native = auto-detect host)")
    args = ap.parse_args()

    if args.list_targets:
        return cmd_list_targets()
    if args.show_host:
        return cmd_show_host()
    if not args.input or not args.output:
        ap.error("input and output are required (or use --list-targets / "
                 "--show-host)")
    return cross_compile(args.input, args.output, args.target,
                         linker_kind=args.linker, keep_c=args.keep_c,
                         dry_run=args.dry_run, hlc=args.hlc,
                         security=args.security,
                         target_feature=args.target_feature)


if __name__ == "__main__":
    sys.exit(main())
