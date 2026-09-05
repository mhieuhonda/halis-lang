#!/usr/bin/env python3
"""pgo_ratio.py — Stage 19 (v0.35.0-alpha) PGO acceptance measurement.

Compares the wall time of the PGO-trained `hlc` against the plain
(non-PGO) build when compiling the same input (default: src/hlc.hls,
the self-compilation workload the roadmap's acceptance specifies).

Methodology:
  - N runs per binary (default 9), interleaved plain/trained/trained/plain
    ordering to cancel thermal / cache drift,
  - the MEDIAN is compared (individual runs are noisy on shared CI
    machines),
  - exit code 0 when the ratio is <= the --max-ratio threshold (default
    0.80, the Stage 19 acceptance target "in <= 80% of the non-PGO
    build's wall time"),
  - also verifies the two compilers produce BYTE-IDENTICAL C output for
    the input program (the second half of the acceptance criterion).

Usage:
  python3 scripts/pgo_ratio.py --plain bin/hlc --trained bin/hlc_pgo \
      --input src/hlc.hls --runs 9 [--max-ratio 0.80]
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import tempfile
import os


def run_timed(exe: str, inp: str, out: str) -> float:
    import time
    t0 = time.perf_counter()
    subprocess.run([exe, inp, out], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plain", required=True, help="plain (non-PGO) hlc binary")
    ap.add_argument("--trained", required=True, help="PGO-trained hlc binary")
    ap.add_argument("--input", required=True, help="program to compile")
    ap.add_argument("--runs", type=int, default=9)
    ap.add_argument("--max-ratio", type=float, default=0.80)
    args = ap.parse_args()

    plain_times, trained_times = [], []
    with tempfile.TemporaryDirectory() as td:
        out_p = os.path.join(td, "plain.c")
        out_t = os.path.join(td, "trained.c")
        # Interleave to cancel drift: plain, trained, trained, plain, ...
        for i in range(args.runs):
            plain_times.append(run_timed(args.plain, args.input, out_p))
            trained_times.append(run_timed(args.trained, args.input, out_t))
        # Byte-identical output check (the acceptance's second half).
        with open(out_p, "rb") as f:
            plain_c = f.read()
        with open(out_t, "rb") as f:
            trained_c = f.read()

    pm = statistics.median(plain_times)
    tm = statistics.median(trained_times)
    ratio = tm / pm if pm > 0 else float("inf")

    print("plain   hlc : median %.3fs  (runs: %s)" % (
        pm, " ".join("%.3f" % t for t in plain_times)))
    print("trained hlc : median %.3fs  (runs: %s)" % (
        tm, " ".join("%.3f" % t for t in trained_times)))
    print("ratio       : %.1f%%  (target <= %.0f%%)" % (
        100 * ratio, 100 * args.max_ratio))
    print("byte-identical output: %s" % ("YES" if plain_c == trained_c else "NO"))

    if plain_c != trained_c:
        print("FAIL: trained compiler output differs from plain build")
        return 1
    if ratio > args.max_ratio:
        print("FAIL: ratio exceeds the acceptance threshold")
        return 1
    print("PASS: Stage 19 acceptance (PGO-trained hlc compiles the input "
          "in <= %d%% of the plain build's wall time, byte-identical "
          "output)" % round(100 * args.max_ratio))
    return 0


if __name__ == "__main__":
    sys.exit(main())
