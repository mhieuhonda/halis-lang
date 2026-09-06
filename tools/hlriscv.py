#!/usr/bin/env python3
"""hlriscv.py — Stage 26 (v0.49.0-alpha): RISC-V 64 backend helper.

Wraps the RISC-V-specific flags (RVV intrinsics, -march/-mabi, the
bare-metal freestanding flags) into a single convenient CLI. Internally
delegates to `tools/hlcross.py` with the right --target and
--target-feature flags.

Features:
  * `--target riscv64gc-unknown-linux-gnu` (default) — full Linux
    user-mode (RV64GC: IMAFDC), SiFive / VisionFive / QEMU riscv64.
  * `--target riscv64-unknown-none` — bare-metal (no OS, no libc, for
    OS-development work). Produces a freestanding ELF that runs in
    QEMU with `-bios none -machine virt`.
  * `--target-feature rvv` (default for the Linux target) — enable the
    RISC-V Vector (RVV) intrinsic fast paths for std.simd kernels
    (the Stage 26 RVV codegen in src/hlc.hls). Promotes -march from
    rv64gc -> rv64gcv (the canonical notation for G + C + V).

Acceptance (Stage 26): `make cross TARGET=riscv64-unknown-none` produces
a bare-metal binary that runs in QEMU; the binary contains zero libc
references. On a RISC-V 64 host (rare in CI):

  python3 tools/hlriscv.py benchmarks/simd_bench.hls /tmp/simd_rvv \\
      --target riscv64gc-unknown-linux-gnu --target-feature rvv

Compare against the baseline (no RVV):

  python3 tools/hlriscv.py benchmarks/simd_bench.hls /tmp/simd_baseline \\
      --target riscv64gc-unknown-linux-gnu --target-feature ""

On non-RISC-V hosts, the cross-compilation to riscv64 still produces
the C source with RVV intrinsics (verified by
`grep -c "vadd_vv_i32m1" <out>.c`), but the runtime benchmark is
skipped (no RISC-V hardware to run on).

Usage:
  python3 tools/hlriscv.py <input.hls> <output.bin>
      [--target riscv64gc-unknown-linux-gnu|riscv64-unknown-none]
      [--target-feature rvv|native|""]
      [--linker auto|zig|gcc|clang|cc]
      [--keep-c PATH]
      [--dry-run]
      [--hlc bin/hlc]
      [--list-targets]
"""
from __future__ import annotations

import argparse
import os
import sys

# Repo root for resolving hlcross.py.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 26 RISC-V 64 backend helper. Wraps hlcross with "
                    "the right -march/-mabi + RVV flags.")
    ap.add_argument("input", nargs="?",
                    help="HLS source file (e.g. benchmarks/simd_bench.hls)")
    ap.add_argument("output", nargs="?", help="output binary path")
    ap.add_argument("--target", default="riscv64gc-unknown-linux-gnu",
                    choices=["riscv64gc-unknown-linux-gnu",
                             "riscv64-unknown-linux-gnu",
                             "riscv64-unknown-none"],
                    help="target triple (default: riscv64gc-unknown-linux-gnu)")
    ap.add_argument("--target-feature", default="rvv",
                    choices=["", "rvv", "native"],
                    help="enable std.simd intrinsic fast paths "
                         "(default: rvv; pass an empty string for none). "
                         "Ignored for the bare-metal target (riscv64-unknown-none)")
    ap.add_argument("--linker", default="auto",
                    choices=["auto", "zig", "gcc", "clang", "cc"],
                    help="linker strategy (default: auto-detect)")
    ap.add_argument("--keep-c", help="keep the intermediate C file at PATH")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands without executing them")
    ap.add_argument("--hlc", default="bin/hlc",
                    help="path to the native hlc compiler (default: bin/hlc)")
    ap.add_argument("--list-targets", action="store_true",
                    help="list the supported RISC-V target triples and exit")
    args = ap.parse_args()

    if args.list_targets:
        print("Supported RISC-V 64 targets (Stage 26):")
        print("  riscv64gc-unknown-linux-gnu  Linux user-mode (RV64GC: IMAFDC)")
        print("                              SiFive / VisionFive / QEMU riscv64")
        print("  riscv64-unknown-linux-gnu   Alias for riscv64gc-unknown-linux-gnu")
        print("  riscv64-unknown-none        Bare-metal (no OS, no libc)")
        print("                              For OS-development work")
        print()
        print("Target features (--target-feature):")
        print("  rvv      Enable RISC-V Vector (RVV) intrinsic fast paths")
        print("           for std.simd (the Stage 26 RVV codegen in src/hlc.hls).")
        print("           Promotes -march from rv64gc -> rv64gcv.")
        print("           Only valid for the Linux target (the bare-metal target")
        print("           uses rv64imac, no V extension).")
        print("  native   Auto-detect the host CPU's best SIMD feature")
        print("  (empty)  No intrinsic fast paths (scalar fallback)")
        print()
        print("Bare-metal flags (riscv64-unknown-none):")
        print("  -march=rv64imac -mabi=lp64 -nostdlib -nostartfiles -ffreestanding")
        print("  The binary runs in QEMU with: qemu-system-riscv64 -bios none \\")
        print("    -machine virt -kernel <binary> -nographic")
        return 0

    if not args.input or not args.output:
        ap.error("input and output are required (or use --list-targets)")

    # The bare-metal target does NOT support the V extension (rv64imac
    # has no V). Strip --target-feature rvv when targeting riscv64-unknown-none.
    target_feature = args.target_feature
    if args.target == "riscv64-unknown-none" and target_feature == "rvv":
        target_feature = ""  # bare-metal: no RVV

    # Delegate to hlcross.cross_compile.
    try:
        from hlcross import cross_compile  # type: ignore
    except ImportError:
        sys.stderr.write("hlriscv: cannot import hlcross.cross_compile\n")
        return 2

    return cross_compile(args.input, args.output, args.target,
                         linker_kind=args.linker, keep_c=args.keep_c,
                         dry_run=args.dry_run, hlc=args.hlc,
                         security="auto",  # RISC-V has no PAC/BTI; "auto" = empty
                         target_feature=target_feature)


if __name__ == "__main__":
    sys.exit(main())
