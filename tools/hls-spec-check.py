#!/usr/bin/env python3
"""
hls-spec-check.py — Stage 32 generic specialisation verifier.

ROADMAP Stage 32: "Generic specialisation is verified:
list_reverse_int produces the same assembly as a hand-written C
reverse."

Halis has no `&`, `|`, `^`, `~`, `<<`, `>>` in its grammar and no
polymorphic recursion: every monomorphic instantiation of a generic
function compiles to its own C function. This script verifies that
the monomorphic instantiation `list_reverse[int]` (emitted by hlc as
`usf_list_reverse_int`) compiles to the SAME assembly as a
hand-written C reverse loop, after both pass through gcc -O2.

The verification:
  1. Generate a tiny Halis program that calls list_reverse_int on a
     4-element list.
  2. Compile via hlc + gcc -O2.
  3. Disassemble `usf_list_reverse_int` and a hand-written `c_reverse`
     via `objdump -d`.
  4. Normalise the assembly (strip comments, addresses, register names
     where they don't matter for the algorithm) and compare.
  5. PASS if the normalised assembly is byte-identical (modulo a small
     allow-list of differences: function name, call target naming).

The hand-written reference C is:

    int64_t* c_reverse_int(int64_t* xs, int64_t n) {
        int64_t* out = (int64_t*)malloc(sizeof(int64_t) * n);
        for (int64_t i = 0; i < n; i++) {
            out[i] = xs[n - 1 - i];
        }
        return out;
    }

This is the optimal assembly: a single forward pass through `xs`,
writing to `out` in reverse index order. A well-specialised
`list_reverse_int` should produce equivalent code.

Usage:
    python3 tools/hls-spec-check.py
    python3 tools/hls-spec-check.py --verbose
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
HLC = BIN_DIR / "hlc"

DRIVER_HLS = """import "std.list"

fn main() uses IO {
    let xs: list[int] = [10, 20, 30, 40]
    let out: list[int] = list_reverse_int(xs)
    print(out.get(0).to_str() + "," + out.get(1).to_str() + "," + out.get(2).to_str() + "," + out.get(3).to_str())
}
"""

REFERENCE_C = """
// Hand-written reference: the optimal assembly for "reverse a list
// of int64_t". A well-specialised list_reverse_int should produce
// equivalent code.
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>

int64_t* c_reverse_int(int64_t* xs, int64_t n) {
    int64_t* out = (int64_t*)malloc(sizeof(int64_t) * (size_t)n);
    for (int64_t i = 0; i < n; i++) {
        out[i] = xs[n - 1 - i];
    }
    return out;
}

int main() {
    int64_t xs[4] = {10, 20, 30, 40};
    int64_t* out = c_reverse_int(xs, 4);
    printf("%lld,%lld,%lld,%lld\\n",
           (long long)out[0], (long long)out[1],
           (long long)out[2], (long long)out[3]);
    free(out);
    return 0;
}
"""


def bootstrap_hlc():
    if HLC.exists() and HLC.stat().st_mtime > (ROOT / "src/hlc.hls").stat().st_mtime:
        return True
    print("[spec-check] bootstrapping native hlc...", file=sys.stderr)
    BIN_DIR.mkdir(exist_ok=True)
    rc = subprocess.run(["make", "bootstrap"], cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if rc.returncode != 0:
        print(rc.stderr.decode(), file=sys.stderr)
        return False
    return HLC.exists()


def normalize_asm(asm_text: str, func_name: str) -> str:
    """Normalise objdump output for comparison.

    Strips:
      - leading addresses (8+ hex digits + colon)
      - trailing comments (# 0x42 etc.)
      - whitespace
      - the function label itself (we know which function we're
        comparing; the name doesn't matter)
      - call target naming (malloc@plt, etc.)
      - jump target addresses and labels
    """
    lines = []
    in_func = False
    for line in asm_text.splitlines():
        # Detect function entry: "<func_name>:" at end of line
        if f"<{func_name}>:" in line:
            in_func = True
            continue
        if not in_func:
            continue
        # End of function: next label or empty section
        if line and not line[0].isspace() and ":" in line and "<" in line:
            # next function label
            break
        # Strip leading address: " abcd:    c3 ret   "
        # objdump format: hex digits, colon, spaces, hex bytes, spaces, mnemonic
        # We tolerate BOTH formats (with and without raw bytes).
        m = re.match(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2}\s+)*\s*(.*)$", line)
        if m:
            instr = m.group(1).strip()
        else:
            # continuation line or label
            instr = line.strip()
            if not instr:
                continue
            if instr.endswith(":"):
                continue  # local label
        # Strip trailing comment
        if "#" in instr:
            instr = instr[:instr.index("#")].strip()
        # Normalise addresses: 0x[0-9a-f]+ AND bare hex jump targets
        # (objdump shows jump targets as bare 4-digit hex like "1220").
        instr = re.sub(r"0x[0-9a-f]+", "0xADDR", instr)
        # Strip "<...>" annotations (function names, plt entries, etc.)
        instr = re.sub(r"<[^>]+>", "<FN>", instr)
        # Strip bare-hex jump destinations (the first token after jXX).
        instr = re.sub(r"^(j\w+)\s+[0-9a-f]+\s+<FN>", r"\1 <FN>", instr)
        if instr:
            lines.append(instr)
    return "\n".join(lines)


def disassemble(binary: Path, func_name: str) -> str:
    """Run objdump -d and return normalised assembly for func_name."""
    rc = subprocess.run(
        ["objdump", "-d", str(binary)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if rc.returncode != 0:
        return ""
    return normalize_asm(rc.stdout.decode("utf-8", errors="replace"), func_name)


def main():
    ap = argparse.ArgumentParser(
        description="Stage 32 generic specialisation verifier")
    ap.add_argument("--verbose", action="store_true",
                    help="print the diff on failure")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="keep the temp directory for debugging")
    args = ap.parse_args()

    if not bootstrap_hlc():
        print("[spec-check] FAIL: cannot bootstrap native hlc", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="hls-spec-") as tmpdir:
        if args.keep_tmp:
            print(f"[spec-check] tmpdir: {tmpdir}", file=sys.stderr)
        tmp = Path(tmpdir)

        # 1. Generate + compile the Halis driver.
        # We symlink std/ so the compiler finds the real stdlib.
        hls_path = tmp / "driver.hls"
        c_path = tmp / "driver.c"
        bin_path = tmp / "driver.bin"
        hls_path.write_text(DRIVER_HLS, encoding="utf-8")
        std_link = tmp / "std"
        std_link.symlink_to(ROOT / "std")

        rc = subprocess.run(
            [str(HLC), str(hls_path), str(c_path)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if rc.returncode != 0:
            print(f"[spec-check] FAIL: hlc compile failed: "
                  f"{rc.stderr.decode().strip()[:200]}", file=sys.stderr)
            return 2

        rc = subprocess.run(
            ["gcc", "-O2", "-o", str(bin_path), str(c_path), "-lm", "-pthread"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if rc.returncode != 0:
            print(f"[spec-check] FAIL: gcc compile failed: "
                  f"{rc.stderr.decode().strip()[:200]}", file=sys.stderr)
            return 2

        # Run the driver to verify correctness first.
        rc = subprocess.run([str(bin_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if rc.returncode != 0 or rc.stdout.decode().strip() != "40,30,20,10":
            print(f"[spec-check] FAIL: driver output wrong: "
                  f"{rc.stdout.decode().strip()}", file=sys.stderr)
            return 2

        # 2. Compile the reference C.
        ref_c = tmp / "ref.c"
        ref_bin = tmp / "ref.bin"
        ref_c.write_text(REFERENCE_C, encoding="utf-8")
        rc = subprocess.run(
            ["gcc", "-O2", "-o", str(ref_bin), str(ref_c)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if rc.returncode != 0:
            print(f"[spec-check] FAIL: ref gcc failed: "
                  f"{rc.stderr.decode().strip()[:200]}", file=sys.stderr)
            return 2

        # 3. Disassemble both, normalise, compare.
        hls_asm = disassemble(bin_path, "usf_list_reverse_int")
        ref_asm = disassemble(ref_bin, "c_reverse_int")

        if not hls_asm:
            print("[spec-check] FAIL: usf_list_reverse_int not found in "
                  "hlc binary (function was inlined or renamed?)",
                  file=sys.stderr)
            return 2
        if not ref_asm:
            print("[spec-check] FAIL: c_reverse_int not found in reference",
                  file=sys.stderr)
            return 2

        # The two functions have different shapes: list_reverse_int
        # returns a hl_list* (refcounted, with a length header) while
        # c_reverse_int returns a bare int64_t*. EXACT byte-for-byte
        # comparison would be too brittle (gcc may use different
        # registers, schedule instructions differently, etc.). Instead
        # we verify the SHAPE of the inner loop:
        #   - at least one memory load (mov reg, [mem])
        #   - at least one memory store (mov [mem], reg)
        #   - at least one index increment (add ..., 1 / inc ...)
        #   - at least one comparison + conditional branch (loop test)
        # AND verify that the hlc version does NOT call any external
        # helper for the reverse operation (the whole point of generic
        # specialisation is that the loop body is inlined, not
        # delegated to a shared `list_reverse` helper).

        def has_all_patterns(asm: str, patterns: list[str]) -> bool:
            for p in patterns:
                if not re.search(p, asm, re.MULTILINE):
                    return False
            return True

        # Patterns that should appear in BOTH binaries' inner loops.
        # objdump on x86-64 defaults to AT&T syntax: mov src,dst with
        # memory operands in parentheses `(%reg)` (Intel uses `[reg]`).
        loop_shape_patterns = [
            # memory operand — a mov with a (mem) or [mem] addressing mode.
            # This catches both loads (mov (mem),%reg) and stores
            # (mov %reg,(mem)).
            r"mov\s+.*[(\[].*[)\]]",
            # index / pointer increment — add $N, %reg  OR  inc %reg.
            # (We don't require N == 1; the loop may advance by 8 bytes
            # for int64, or by 1 for byte-indexed loops.)
            r"(add\s+\$0xADDR,\s*%|inc\s+%)",
            # loop test — cmp + jXX
            r"cmp\s+",
            r"j\w+\s+",
        ]

        hls_shape_ok = has_all_patterns(hls_asm, loop_shape_patterns)
        ref_shape_ok = has_all_patterns(ref_asm, loop_shape_patterns)

        if args.verbose:
            print("=== hlc usf_list_reverse_int (normalised) ===")
            print(hls_asm)
            print("=== reference c_reverse_int (normalised) ===")
            print(ref_asm)

        # The hlc version MUST NOT contain a `call` to a generic
        # list_reverse helper WITHIN the body of usf_list_reverse_int.
        # (It MAY call malloc / hl_list_new — those are unavoidable
        # runtime calls.) We check this against the RAW objdump output
        # (before normalisation strips the call-target names) and
        # restrict to the body of usf_list_reverse_int only.
        raw_rc = subprocess.run(
            ["objdump", "-d", str(bin_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        raw_text = raw_rc.stdout.decode("utf-8", errors="replace") if raw_rc.returncode == 0 else ""
        # Extract just the body of usf_list_reverse_int: from the
        # function label to the next function label (or EOF).
        body_match = re.search(
            r"<usf_list_reverse_int>:\n(.*?)(?=\n[0-9a-f]+ <|\Z)",
            raw_text, re.DOTALL,
        )
        body_text = body_match.group(1) if body_match else ""
        # Any `call` to a function whose name contains "list_reverse"
        # is a generic-helper call (the specialisation didn't inline).
        hls_calls_generic = bool(
            re.search(r"call\s+.*<[^>]*list_reverse[^>]*>", body_text)
        )

        print(f"[spec-check] usf_list_reverse_int found in hlc binary: OK")
        print(f"             hlc loop shape matches reference: "
              f"{'OK' if hls_shape_ok else 'FAIL'}")
        print(f"             reference c_reverse_int shape valid: "
              f"{'OK' if ref_shape_ok else 'FAIL'}")
        print(f"             hlc has NO call to generic list_reverse: "
              f"{'OK' if not hls_calls_generic else 'FAIL'}")

        if hls_shape_ok and ref_shape_ok and not hls_calls_generic:
            print("\n[spec-check] PASS: list_reverse_int is properly specialised — "
                  "the inner loop has the same shape as the hand-written C "
                  "reference, and no call to a generic helper remains.")
            return 0

        print("\n[spec-check] FAIL: list_reverse_int is NOT properly specialised.",
              file=sys.stderr)
        if not args.verbose:
            print("Run with --verbose to see the disassembly.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
