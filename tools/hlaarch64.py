#!/usr/bin/env python3
"""hlaarch64.py — Stage 25 (v0.44.0-alpha): AArch64 backend tuning helper.

Wraps the AArch64-specific tuning flags (NEON intrinsics, PAC, BTI) into
a single convenient CLI. Internally delegates to `tools/hlcross.py` with
the right --target and --security flags.

Features:
  * `--target aarch64-linux-gnu` (default) — Graviton 3+ / RPi 4 / etc.
  * `--target aarch64-apple-darwin` — Apple Silicon (M1/M2/M3).
  * `--target-feature neon` — enable NEON intrinsic fast paths for
    std.simd kernels (the Stage 25 NEON codegen in src/hlc.hls).
  * `--security auto` (default) — use the target's default security
    flags (pac-ret+bti on Apple Silicon, bti on Graviton 3+).
  * `--security pac+bti` — force full PAC + BTI (Graviton 4, Apple M1+).
  * `--security bti` — force BTI only (Graviton 3+ baseline).
  * `--security off` — disable security hardening (faster, less secure).

Acceptance (Stage 25): `benchmarks/json_bench.hls` runs >=20% faster on
AArch64 than the v0.34 baseline. On AArch64 hosts, run:

  python3 tools/hlaarch64.py benchmarks/json_bench.hls /tmp/json_neon \\
      --target-feature neon --security pac+bti

Compare against the baseline (no NEON, no PAC/BTI):

  python3 tools/hlaarch64.py benchmarks/json_bench.hls /tmp/json_baseline \\
      --target-feature "" --security off

On x86_64 hosts, the cross-compilation to aarch64 still produces the C
source with NEON intrinsics (verified by `grep -c "vaddq_s32" <out>.c`),
but the runtime benchmark is skipped (no AArch64 hardware to run on).

Usage:
  python3 tools/hlaarch64.py <input.hls> <output.bin>
      [--target aarch64-linux-gnu|aarch64-apple-darwin]
      [--target-feature neon|sse4.2|avx2|native|""]
      [--security auto|pac+bti|bti|off]
      [--linker auto|zig|gcc|clang|cc]
      [--keep-c PATH]
      [--dry-run]
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
        description="Stage 25 AArch64 backend tuning helper. Wraps "
                    "hlcross with the right NEON + PAC/BTI flags.")
    ap.add_argument("input", nargs="?", help="HLS source file (e.g. benchmarks/json_bench.hls)")
    ap.add_argument("output", nargs="?", help="output binary path")
    ap.add_argument("--target", default="aarch64-linux-gnu",
                    choices=["aarch64-linux-gnu", "aarch64-unknown-linux-gnu",
                             "aarch64-apple-darwin"],
                    help="target triple (default: aarch64-linux-gnu)")
    ap.add_argument("--target-feature", default="neon",
                    choices=["", "neon", "native"],
                    help="enable std.simd intrinsic fast paths "
                         "(default: neon; pass an empty string for none)")
    ap.add_argument("--security", default="auto",
                    choices=["auto", "pac+bti", "bti", "off"],
                    help="AArch64 security hardening "
                         "(default: auto = use target's default)")
    ap.add_argument("--linker", default="auto",
                    choices=["auto", "zig", "gcc", "clang", "cc"],
                    help="linker strategy (default: auto-detect)")
    ap.add_argument("--keep-c", help="keep the intermediate C file at PATH")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands without executing them")
    ap.add_argument("--hlc", default="bin/hlc",
                    help="path to the native hlc compiler (default: bin/hlc)")
    ap.add_argument("--list-targets", action="store_true",
                    help="list the supported AArch64 target triples and exit")
    args = ap.parse_args()

    if args.list_targets:
        print("Supported AArch64 targets (Stage 25):")
        print("  aarch64-linux-gnu          Graviton 3+ / RPi 4 (Linux, glibc)")
        print("  aarch64-unknown-linux-gnu  Alias for aarch64-linux-gnu")
        print("  aarch64-apple-darwin       Apple Silicon (M1/M2/M3)")
        print()
        print("Security flags (--security):")
        print("  auto      Use target's default (pac-ret+bti on Apple Silicon,")
        print("            bti on Graviton 3+)")
        print("  pac+bti   Force full PAC + BTI (Graviton 4, Apple M1+)")
        print("  bti       Force BTI only (Graviton 3+ baseline)")
        print("  off       Disable security hardening (faster, less secure)")
        print()
        print("Target features (--target-feature):")
        print("  neon      Enable NEON intrinsic fast paths for std.simd")
        print("            (the Stage 25 NEON codegen in src/hlc.hls)")
        print("  native    Auto-detect the host CPU's best SIMD feature")
        print("  (empty)   No intrinsic fast paths (scalar fallback)")
        return 0

    if not args.input or not args.output:
        ap.error("input and output are required (or use --list-targets)")

    # Delegate to hlcross.cross_compile.
    try:
        from hlcross import cross_compile  # type: ignore
    except ImportError:
        sys.stderr.write("hlaarch64: cannot import hlcross.cross_compile\n")
        return 2

    return cross_compile(args.input, args.output, args.target,
                         linker_kind=args.linker, keep_c=args.keep_c,
                         dry_run=args.dry_run, hlc=args.hlc,
                         security=args.security,
                         target_feature=args.target_feature)


if __name__ == "__main__":
    sys.exit(main())
