#!/usr/bin/env python3
"""hlpgo.py — Stage 19 perfection (v0.38.0-alpha): PGO profile utilities.

Companion to the `--pgo-generate` / `--pgo-use` machinery in
`src/hlc.hls`. Provides three offline operations on .hlcprof files
that the in-compiler CLI does not expose:

  hlpgo report <profile>           Print a hotness report (top-N
                                    functions by entry count, branch
                                    bias summary, loop back-edge
                                    counts, total call volume).
  hlpgo merge  <out> <in1> <in2>.. Merge multiple .hlcprof files
                                    (sums per-site counters; the same
                                    operation the runtime does under
                                    HLS_PGO_MERGE=1, but offline).
  hlpgo diff   <p1> <p2>           Diff two profiles (per-site delta;
                                    useful for verifying training
                                    stability across runs).

The profile format is line-based plain text:
    <site_id> <count>
where <site_id> is one of:
    e:<fnkey>           function entry
    b:<fnkey>:<n>       branch (n is the per-function branch index)
    l:<fnkey>:<n>       loop back-edge (n is the per-function loop index)

A forward-compatible magic header is recognised:
    # hlcprof v1
If present, it is skipped (and the version is reported). Files without
the header are read as raw v0 profiles (the original Stage 19 release
format) — backward compatible.

Usage:
  python3 tools/hlpgo.py report path/to/foo.hlcprof [--top N]
  python3 tools/hlpgo.py merge  out.hlcprof in1.hlcprof in2.hlcprof ...
  python3 tools/hlpgo.py diff   p1.hlcprof p2.hlcprof [--min-delta N]
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Tuple


MAGIC_HEADER = "# hlcprof v1"


# ---------------------------------------------------------------------------
# Profile parsing
# ---------------------------------------------------------------------------

def parse_profile(path: str) -> Tuple[Dict[str, int], str]:
    """Parse a .hlcprof file. Returns (counts, version).

    `version` is "v1" when the magic header is present, "v0" otherwise
    (the original Stage 19 release format — no header). The parser is
    backward compatible: a v0 file parses identically to a v1 file
    without the header line.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"profile not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    version = "v0"
    counts: Dict[str, int] = {}
    first_line = text.split("\n", 1)[0]
    if first_line.strip() == MAGIC_HEADER:
        version = "v1"
        text = text.split("\n", 1)[1] if "\n" in text else ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError(
                f"malformed profile line in {path}: {raw!r}")
        site_id, count_str = parts
        try:
            count = int(count_str)
        except ValueError:
            raise ValueError(
                f"malformed count for site '{site_id}' in {path}: "
                f"{count_str!r}")
        counts[site_id] = counts.get(site_id, 0) + count
    return counts, version


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    counts, version = parse_profile(args.profile)
    if not counts:
        print(f"{args.profile}: empty profile (no sites)", file=sys.stderr)
        return 1
    entries: List[Tuple[str, int]] = sorted(
        counts.items(), key=lambda kv: -kv[1])
    total_calls = sum(
        c for sid, c in counts.items() if sid.startswith("e:"))
    branch_sites = [s for s in counts if s.startswith("b:")]
    loop_sites = [s for s in counts if s.startswith("l:")]
    fn_sites = [s for s in counts if s.startswith("e:")]

    print(f"profile: {args.profile}")
    print(f"format : {version} ({MAGIC_HEADER if version == 'v1' else 'no header'})")
    print(f"sites  : {len(counts)} total "
          f"({len(fn_sites)} fn, {len(branch_sites)} branch, "
          f"{len(loop_sites)} loop)")
    print(f"calls  : {total_calls} (sum of fn-entry counts)")
    print()
    top_n = args.top
    print(f"top {top_n} hottest functions (by entry count):")
    fn_entries = [(sid[2:], c) for sid, c in entries
                  if sid.startswith("e:")]
    for i, (fn, c) in enumerate(fn_entries[:top_n]):
        pct = (100.0 * c / total_calls) if total_calls else 0.0
        print(f"  {i+1:4d}. {c:>12d}  {pct:5.1f}%  {fn}")
    print()
    print("branch bias summary (true-count / false-count):")
    # Group branches by function for readability.
    by_fn: Dict[str, List[Tuple[int, int, int]]] = {}
    for sid in branch_sites:
        # b:<fnkey>:<n>
        rest = sid[2:]
        if ":" not in rest:
            continue
        fn, n_str = rest.rsplit(":", 1)
        try:
            n = int(n_str)
        except ValueError:
            continue
        # Branches come in true/false pairs: b:<fn>:<n> (true) and
        # b:<fn>:<n>_f (false). The original Stage 19 release emits
        # only the true-arm counter; the false count is inferred as
        # entry_count - true_count.
        if sid.endswith("_f"):
            continue
        true_c = counts.get(sid, 0)
        entry_c = counts.get("e:" + fn, 0)
        false_c = max(entry_c - true_c, 0)
        by_fn.setdefault(fn, []).append((n, true_c, false_c))
    for fn in sorted(by_fn):
        for n, t, f in sorted(by_fn[fn]):
            total = t + f
            if total == 0:
                continue
            bias = "true" if t >= f else "false"
            print(f"  {fn}:{n}  true={t:>8d}  false={f:>8d}  "
                  f"({100.0*t/total:5.1f}% true, bias={bias})")
    print()
    print("loop back-edge counts:")
    loop_entries = [(sid, counts[sid]) for sid in loop_sites]
    loop_entries.sort(key=lambda x: -x[1])
    for sid, c in loop_entries[:top_n]:
        print(f"  {c:>12d}  {sid}")
    return 0


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace) -> int:
    merged: Dict[str, int] = {}
    for in_path in args.inputs:
        counts, _ = parse_profile(in_path)
        for sid, c in counts.items():
            merged[sid] = merged.get(sid, 0) + c
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(MAGIC_HEADER + "\n")
        for sid in sorted(merged):
            f.write(f"{sid} {merged[sid]}\n")
    print(f"merged {len(args.inputs)} profile(s) "
          f"({len(merged)} unique sites) -> {args.output}")
    return 0


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def cmd_diff(args: argparse.Namespace) -> int:
    c1, _ = parse_profile(args.p1)
    c2, _ = parse_profile(args.p2)
    all_sites = sorted(set(c1) | set(c2))
    min_delta = args.min_delta
    diff_lines: List[str] = []
    for sid in all_sites:
        a = c1.get(sid, 0)
        b = c2.get(sid, 0)
        delta = b - a
        if abs(delta) < min_delta:
            continue
        diff_lines.append(f"  {sid:<40s}  {a:>10d} -> {b:>10d}  "
                          f"(delta {delta:+d})")
    if not diff_lines:
        print(f"no sites differ by >= {min_delta} "
              f"between {args.p1} and {args.p2}")
        return 0
    print(f"sites that differ by >= {min_delta} "
          f"({args.p1} -> {args.p2}):")
    for line in diff_lines:
        print(line)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="PGO profile utilities (Stage 19 perfection).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser(
        "report", help="print a hotness report for a .hlcprof file")
    p_report.add_argument("profile", help="path to .hlcprof")
    p_report.add_argument("--top", type=int, default=20,
                          help="top-N entries to show (default 20)")
    p_report.set_defaults(func=cmd_report)

    p_merge = sub.add_parser(
        "merge", help="merge multiple .hlcprof files into one")
    p_merge.add_argument("output", help="output path")
    p_merge.add_argument("inputs", nargs="+", help="input profiles")
    p_merge.set_defaults(func=cmd_merge)

    p_diff = sub.add_parser(
        "diff", help="diff two .hlcprof files (per-site delta)")
    p_diff.add_argument("p1", help="first profile")
    p_diff.add_argument("p2", help="second profile")
    p_diff.add_argument("--min-delta", type=int, default=1,
                        help="minimum |delta| to report (default 1)")
    p_diff.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
