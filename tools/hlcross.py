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
    # Stage 26 (v0.49.0-alpha): RISC-V 64 targets.
    # riscv64gc-unknown-linux-gnu — full Linux user-mode (RV64GC: IMAFDC).
    # The 'gc' suffix denotes the G (IMAFD) + C (compressed) extensions;
    # this is the standard Linux RISC-V target (SiFive Freedom, VisionFive,
    # QEMU riscv64). The C backend is portable ANSI C11; cross-compilation
    # reduces to picking the right cross-linker (zig cc, the Debian/Ubuntu
    # riscv64-linux-gnu-gcc cross-toolchain) and the right -march/-mabi.
    "riscv64gc-unknown-linux-gnu": {
        "arch": "riscv64",
        "os": "linux",
        "abi": "gnu",
        "binary_format": "ELF riscv64 (Little Endian, RV64GC)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm", "-lpthread"],
        "mingw": False,
        # -march=rv64gc -mabi=lp64d: the standard Linux user-mode profile
        # (G = IMAFD, C = compressed instructions; lp64d = 64-bit LP64
        # with hardware double-float ABI). The Stage 26 RVV (V) extension
        # is enabled separately via --target-feature rvv -> -march=rv64gcv.
        "march": "rv64gc",
        "mabi": "lp64d",
        "security_flags": [],
    },
    "riscv64-unknown-linux-gnu": {
        # Alias for riscv64gc-unknown-linux-gnu (canonical Linux RISC-V 64).
        "arch": "riscv64",
        "os": "linux",
        "abi": "gnu",
        "binary_format": "ELF riscv64 (Little Endian, RV64GC)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": ["-lm", "-lpthread"],
        "mingw": False,
        "march": "rv64gc",
        "mabi": "lp64d",
        "security_flags": [],
    },
    # riscv64-unknown-none — bare-metal (no OS, no libc, for OS work).
    # This is the foundation target for RISC-V OS development: a freestanding
    # ELF with no libc references, suitable for booting in QEMU with
    # `-bios none -machine virt` and jumping to the entry point. The C
    # backend emits ANSI C11 with no libc calls (no printf, no malloc) —
    # every runtime symbol is provided by the Halis C runtime
    # (hl_*, usf_*, hl_box_*, hl_unbox_*), which the cross-linker
    # resolves against the program itself (statically linked, no
    # shared libraries). The _start entry point is supplied by a
    # bare-metal linker script + crt0 (Stage 77+ provides the
    # `#![freestanding]` mode; Stage 26 produces the binary that runs
    # in QEMU with zero libc references).
    "riscv64-unknown-none": {
        "arch": "riscv64",
        "os": "none",
        "abi": "none",
        "binary_format": "ELF riscv64 (Little Endian, RV64IMAC, freestanding)",
        "object_suffix": ".o",
        "binary_suffix": "",
        # Bare-metal: no link libs at all (no libc, no libm, no pthread).
        # The Halis C runtime provides every symbol the program references.
        "link_libs": [],
        "mingw": False,
        # Bare-metal RV64IMAC: I (base integer) + M (mul/div) + A (atomics)
        # + C (compressed) — the standard freestanding profile (no F/D
        # because a kernel typically does NOT enable the FPU until it has
        # saved the FCS/FRR state; the Stage 26 acceptance binary uses
        # integer-only code). mabi=lp64 (no float ABI).
        "march": "rv64imac",
        "mabi": "lp64",
        "security_flags": [],
        # freestanding: -nostdlib -nostartfiles prevents the linker from
        # pulling in crt0/libc; the program provides its own _start.
        "freestanding": True,
    },
    # Stage 93 (v0.112.0-alpha): x86_64-unknown-none — the bare-metal
    # x86-64 triple (roadmap Stage 93). Same shape as the riscv64 one:
    # a freestanding ELF with no libc, linked against the program's own
    # runtime, entering through the naked `_start` the C backend emits
    # (Stage 77). The FP guard is -mgeneral-regs-only: a kernel has not
    # saved the FPU state, so the compiler must not emit SSE; the
    # checker already rejects float literals/annotations under --target
    # (the integer-only discipline). The link base is 1 MiB — where a
    # Multiboot2 bootloader or `qemu-system-x86_64 -kernel` puts the
    # image — and targets/x86_64-unknown-none.ld places .text._start
    # first so the base address IS the entry.
    "x86_64-unknown-none": {
        "arch": "x86_64",
        "os": "none",
        "abi": "none",
        "binary_format": "ELF x86-64 (freestanding, no-PIE)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": [],
        "mingw": False,
        # No -march: the baseline x86-64 ISA (SSE2 excluded by
        # -mgeneral-regs-only anyway) is what a kernel targets first.
        "security_flags": [],
        "freestanding": True,
        "general_regs_only": True,
    },
    # Stage 94 (v0.113.0-alpha): aarch64-unknown-none — the bare-metal
    # ARM triple (roadmap Stage 94). QEMU's `virt` machine is the
    # reference board: DRAM at 0x40000000, the PL011 console at
    # 0x09000000, `-bios none -kernel image.elf` for the boot. Same
    # shape as the x86-64 one — no libc, no crt0, integer-only (the
    # FPU/NEON state is not saved until the kernel does it, and
    # -mgeneral-regs-only makes the compiler refuse to emit FP).
    "aarch64-unknown-none": {
        "arch": "aarch64",
        "os": "none",
        "abi": "none",
        "binary_format": "ELF aarch64 (Little Endian, freestanding)",
        "object_suffix": ".o",
        "binary_suffix": "",
        "link_libs": [],
        "mingw": False,
        "security_flags": [],
        "freestanding": True,
        "general_regs_only": True,
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
    # Stage 26 (v0.49.0-alpha): RISC-V 64 aliases.
    "riscv64-linux": "riscv64gc-unknown-linux-gnu",
    "riscv64": "riscv64gc-unknown-linux-gnu",
    "riscv64gc": "riscv64gc-unknown-linux-gnu",
    "riscv": "riscv64gc-unknown-linux-gnu",
    "riscv64-bare": "riscv64-unknown-none",
    "riscv64-unknown": "riscv64-unknown-none",
    "riscv-bare": "riscv64-unknown-none",
    "riscv-none": "riscv64-unknown-none",
    "visionfive": "riscv64gc-unknown-linux-gnu",
    "sifive": "riscv64gc-unknown-linux-gnu",
    # Stage 93 (v0.112.0-alpha): bare-metal x86-64 aliases.
    "x86_64-none": "x86_64-unknown-none",
    "x86_64-bare": "x86_64-unknown-none",
    "x86-bare": "x86_64-unknown-none",
    "bare-x86_64": "x86_64-unknown-none",
    # Stage 94 (v0.113.0-alpha): bare-metal AArch64 aliases.
    "aarch64-none": "aarch64-unknown-none",
    "aarch64-bare": "aarch64-unknown-none",
    "bare-aarch64": "aarch64-unknown-none",
    "arm64-bare": "aarch64-unknown-none",
}


def canonical_target(name: str) -> str:
    """Resolve a target alias to its canonical triple."""
    if name in TARGETS:
        return name
    if name in TARGET_ALIASES:
        return TARGET_ALIASES[name]
    raise ValueError(f"unknown target '{name}'. Use --list-targets to see "
                     f"the supported set.")


def _repo_root() -> str:
    """The halis-lang repository root (hlcross.py lives in tools/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        # Stage 26 (v0.49.0-alpha): RISC-V 64 host (e.g. VisionFive 2,
        # SiFive Unmatched, QEMU riscv64). Detect both the canonical
        # 'riscv64' machine name and the rarely-used 'rv64gcv' name.
        if machine in ("riscv64", "rv64", "riscv"):
            return "riscv64gc-unknown-linux-gnu"
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
    TARGETS[target]  # KeyError side effect: validates the target name.
    # 1. zig cc — the universal linker.
    zig = find_zig()
    if zig:
        # Stage 93-95: zig spells the bare-metal OS `freestanding`, not
        # `unknown-none` — and it only rejects the wrong spelling at
        # LINK time (a -E or -c run parses the triple loosely). Translate
        # the Rust-style triple for the bare-metal set; every hosted
        # triple passes through unchanged (Stage 22/25/26 behavior).
        zig_triple = target
        if target.endswith("-unknown-none"):
            zig_triple = target[: -len("-unknown-none")] + "-freestanding-none"
        return (zig, ["cc", "-target", zig_triple, "-O2"], "zig")

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

    # Stage 26 (v0.49.0-alpha): RISC-V 64 cross-linkers.
    # Linux user-mode (riscv64gc-unknown-linux-gnu): Debian/Ubuntu ships
    # `riscv64-linux-gnu-gcc`. Bare-metal (riscv64-unknown-none): the
    # `riscv64-unknown-elf-gcc` toolchain (the official RISC-V GNU
    # toolchain, github.com/riscv-collab/riscv-gnu-toolchain, configured
    # with --with-arch=rv64imac --with-abi=lp64 — no libc, no OS).
    if target in ("riscv64gc-unknown-linux-gnu",
                  "riscv64-unknown-linux-gnu"):
        for name in ("riscv64-linux-gnu-gcc",
                     "riscv64-linux-gnu-gcc-13",
                     "riscv64-linux-gnu-gcc-12",
                     "riscv64-linux-gnu-cc",
                     "riscv64-unknown-linux-gnu-gcc"):
            p = _which(name)
            if p:
                return (p, ["-O2"], "riscv64-linux-gnu-gcc")
    if target == "riscv64-unknown-none":
        # Bare-metal cross-linker (riscv64-unknown-elf-gcc).
        for name in ("riscv64-unknown-elf-gcc",
                     "riscv64-elf-gcc",
                     "riscv64-none-elf-gcc",
                     "riscv64-linux-gnu-gcc"):
            # The last fallback (riscv64-linux-gnu-gcc) is used with
            # -nostdlib -nostartfiles — it produces a freestanding ELF
            # when the Linux toolchain is the only one installed.
            p = _which(name)
            if p:
                return (p, ["-O2"], "riscv64-unknown-elf-gcc")

    # Stage 93 (v0.112.0-alpha): the bare-metal x86-64 triple. A real
    # cross-toolchain first (x86_64-elf-*), then the HOST compiler —
    # linking a static freestanding image needs nothing from the OS,
    # so on an x86-64 host the ordinary gcc works as the linker.
    if target == "x86_64-unknown-none":
        for name in ("x86_64-unknown-elf-gcc",
                     "x86_64-elf-gcc",
                     "x86_64-none-elf-gcc"):
            p = _which(name)
            if p:
                return (p, ["-O2"], "x86_64-elf-gcc")
        host = host_triple()
        if host.startswith("x86_64"):
            for name in ("gcc", "clang", "cc"):
                p = _which(name)
                if p:
                    return (p, ["-O2"], "native-freestanding")

    # Stage 94 (v0.113.0-alpha): the bare-metal AArch64 triple. The
    # bare-metal toolchains first (aarch64-*-elf-gcc), then the Linux
    # cross-compiler with the freestanding flags (it links a no-libc
    # image fine — libc is simply never requested), then zig cc (step
    # 1 above).
    if target == "aarch64-unknown-none":
        for name in ("aarch64-none-elf-gcc",
                     "aarch64-elf-gcc",
                     "aarch64-unknown-elf-gcc",
                     "aarch64-linux-gnu-gcc",
                     "aarch64-linux-gnu-gcc-12",
                     "aarch64-linux-gnu-cc"):
            p = _which(name)
            if p:
                return (p, ["-O2"], "aarch64-elf-gcc")

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
                f"  - x86_64-pc-windows-msvc: requires Windows + MSVC build tools\n"
                f"  - aarch64-linux-gnu:      apt install gcc-aarch64-linux-gnu\n"
                f"  - riscv64gc-unknown-linux-gnu: apt install gcc-riscv64-linux-gnu\n"
                f"  - riscv64-unknown-none:  build the RISC-V GNU toolchain from\n"
                f"                            github.com/riscv-collab/riscv-gnu-toolchain\n"
                f"                            (./configure --with-arch=rv64imac --with-abi=lp64)\n"
                f"  - x86_64-unknown-none:   any x86-64 host gcc links the image\n"
                f"                            (a static freestanding link needs no\n"
                f"                            OS support); x86_64-elf-gcc for a real\n"
                f"                            cross-toolchain\n"
                f"  - aarch64-unknown-none:  apt install gcc-aarch64-linux-gnu\n"
                f"                            (or the bare-metal aarch64-none-elf\n"
                f"                            toolchain)")
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
                  target_feature: str = "",
                  debug: bool = False) -> int:
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
            print("error: --linker zig requested but zig is not installed",
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
    # Stages 93-95: pass --target through for the bare-metal triples —
    # the flag implies #![freestanding] and turns on the integer-only
    # discipline in the compiler itself, and stamps the triple into the
    # emitted C (the discipline is a property of the BUILD, so the
    # orchestrator must never compile a bare-metal image without it).
    hlc_cmd = [hlc, input_hls, c_path]
    if spec.get("freestanding", False):
        hlc_cmd += ["--target", target]
    if target_feature:
        hlc_cmd.append("--target-feature")
        hlc_cmd.append(target_feature)
    # Stage 96 (v0.115.0-alpha): --debug passes through to hlc (the C
    # carries the #line mapping) and turns on DWARF 5 in the C compile
    # — the resulting image keeps its debug info instead of the
    # default strip-to-size build.
    if debug:
        hlc_cmd.append("--debug")
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
    # Stage 26 (v0.49.0-alpha): RISC-V -march/-mabi flags.
    # riscv64gc-unknown-linux-gnu -> -march=rv64gc -mabi=lp64d (or rv64gcv
    #   when --target-feature rvv is given: the V extension is added
    #   to the -march string so the cross-linker accepts the RVV intrinsics).
    # riscv64-unknown-none -> -march=rv64imac -mabi=lp64 (bare-metal) +
    #   -nostdlib -nostartfiles -ffreestanding (the program provides its
    #   own _start + every runtime symbol; no libc, no crt0).
    # Stage 93 (v0.112.0-alpha): the bare-metal handling is now GENERIC.
    # Any spec with freestanding=True gets the no-libc link; one with
    # general_regs_only=True gets -mgeneral-regs-only (no SSE/NEON —
    # the integer-only discipline at the compiler-flag level); and when
    # targets/<triple>.ld ships with the repo it drives the placement
    # (-T) so the image is loadable at its fixed base with _start first.
    arch_flags = []
    freestanding_flags = []
    if spec.get("freestanding", False):
        freestanding_flags = ["-nostdlib", "-nostartfiles", "-ffreestanding",
                              "-fno-stack-protector", "-fno-pie", "-no-pie",
                              "-ffunction-sections", "-Wl,--gc-sections"]
        script = os.path.join(_repo_root(), "targets", target + ".ld")
        if os.path.exists(script):
            freestanding_flags.append("-Wl,-T," + script)
    if spec.get("general_regs_only", False):
        arch_flags.append("-mgeneral-regs-only")
    if target in ("riscv64gc-unknown-linux-gnu",
                  "riscv64-unknown-linux-gnu"):
        march = spec.get("march", "rv64gc")
        # When the user requested the V extension via --target-feature rvv,
        # promote -march from rv64gc -> rv64gcv (the canonical notation for
        # "G + C + V"). This is what the RVV intrinsics in <riscv_vector.h>
        # require to compile.
        if target_feature == "rvv":
            march = "rv64gcv"
        arch_flags = ["-march=" + march, "-mabi=" + spec.get("mabi", "lp64d")] + arch_flags
    elif target == "riscv64-unknown-none":
        # Stage 95 (v0.114.0-alpha): rv64imac/lp64 — the integer-only
        # bare-metal ISA (no F/D: the ISA itself is the FP backstop).
        # GCC's -march is a CPU-ISA string; zig cc rejects it ("unknown
        # CPU") — for zig the -mabi alone pins the soft-float ABI and
        # the integer-only code brings no FP instructions anyway.
        if kind != "zig":
            arch_flags = ["-march=" + spec.get("march", "rv64imac")] + arch_flags
        arch_flags = ["-mabi=" + spec.get("mabi", "lp64")] + arch_flags
    # Stage 96: --debug — DWARF 5 debug info in the image. gcc/clang
    # spell it the same; zig cc also accepts both. The mapping lives in
    # the C (hlc --debug); this flag decides whether the image keeps
    # it.
    debug_flags = ["-g", "-gdwarf-5"] if debug else []
    cmd = ([linker] + base_args + sec_flags + arch_flags + debug_flags
           + freestanding_flags + [c_path, "-o", out_path]
           + spec["link_libs"])
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
    print("Supported cross-compilation targets (Stage 22, extended Stage 25 + 26, "
          "bare-metal Stages 93-95):")
    print()
    for triple, spec in TARGETS.items():
        print(f"  {triple}")
        print(f"      arch: {spec['arch']}, os: {spec['os']}, abi: {spec['abi']}")
        print(f"      format: {spec['binary_format']}")
        if "march" in spec:
            print(f"      march: {spec['march']}, mabi: {spec['mabi']}")
        if spec.get("freestanding", False):
            print("      freestanding: -nostdlib -nostartfiles -ffreestanding")
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
    # Stage 26 (v0.49.0-alpha): --target-feature rvv enables the RISC-V
    # Vector (RVV) intrinsic fast paths for std.simd kernels. Also
    # promotes the cross-linker -march from rv64gc to rv64gcv.
    ap.add_argument("--target-feature", default="",
                    choices=["", "neon", "sse4.2", "avx2", "native", "rvv"],
                    help="Stage 25/26: enable std.simd intrinsic fast paths "
                         "(neon for AArch64; sse4.2/avx2 for x86; "
                         "rvv for RISC-V 64; native = auto-detect host)")
    # Stage 96 (v0.115.0-alpha): --debug — the C keeps the #line mapping
    # to the .hls source and the image is linked with -g -gdwarf-5, so
    # DWARF 5 names Halis lines (gdb, addr2line, objdump -S).
    ap.add_argument("--debug", action="store_true",
                    help="Stage 96: keep DWARF 5 debug info in the image "
                         "(hlc --debug + -g -gdwarf-5)")
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
                         target_feature=args.target_feature,
                         debug=args.debug)


if __name__ == "__main__":
    sys.exit(main())
