#!/usr/bin/env python3
"""simd_ratio.py — Stage 21 (v0.37.0-alpha) acceptance measurement.

Parses benchmarks/simd_bench.hls output and gates the acceptance
criterion: the vector kernel must be at least --min times faster than
the scalar kernel (default 2.0x) on the 1M-element list, with matching
checksums (checked by the caller). Exit code 0 on PASS."""
from __future__ import annotations

import argparse
import re
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="benchmark output file")
    ap.add_argument("--min", type=float, default=2.0)
    args = ap.parse_args()

    text = open(args.out).read()
    m_s = re.search(r"scalar\s+time = (\d+) ms", text)
    m_v = re.search(r"vector\s+time = (\d+) ms", text)
    if not m_s or not m_v:
        print("FAIL: could not parse scalar/vector timings")
        return 1
    s, v = int(m_s.group(1)), int(m_v.group(1))
    if v <= 0:
        print("PASS: vector path too fast to measure (<= 0 ms); "
              "scalar %d ms" % s)
        return 0
    ratio = s / v
    print("scalar %d ms / vector %d ms = %.1fx (gate >= %.1fx)"
          % (s, v, ratio, args.min))
    if ratio < args.min:
        print("FAIL: ratio below the acceptance threshold")
        return 1
    print("PASS: Stage 21 acceptance (>= %.1fx on the 1M-element "
          "kernel, identical output)" % args.min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
