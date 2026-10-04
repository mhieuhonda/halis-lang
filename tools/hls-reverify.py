#!/usr/bin/env python3
"""hls-reverify — Stage 108 memory-safety re-verification under
`-O fast` (proof replay).

Usage:
  python3 tools/hls-reverify.py FILE [--lto] [--json] [--out DIR]
  python3 tools/hls-reverify.py selftest

Under `-O fast`, the compiler ELIDES runtime memory-safety checks —
bounds, division-by-zero, int64 overflow — wherever the interval
prover PROVED them dead. The proofs are computed once, on the checked
AST. Everything downstream — the HLIR optimiser (constant folding,
copy propagation, DCE, inlining, LICM), every backend — is trusted to
preserve them. That trust is exactly the kind of hole a verifier
exists to close.

hls-reverify closes it with a REPLAY:

  1. BASELINE — the checked program's memory-safety evidence: every
     check site (ovf / div / bnd) with the interval prover's verdict.
     Proven sites are the claims `-O fast` may elide; checked sites
     keep their runtime check.

  2. TRANSFORM — the program is lowered to HLIR and run through the
     optimiser (fast=True; --lto raises the cross-crate inline
     threshold), exactly as a fast build would.

  3. REPLAY — an independent abstract interpreter re-derives a verdict
     for every check site the TRANSFORMED IR still contains: the same
     seeds (the requires clauses), the same interval arithmetic
     (boot.proof is imported, never re-implemented — one definition),
     the same precision discipline (joins at merges, widening at loop
     headers, a post-fixpoint containment verification). Plus the
     integrity of the transformed IR itself: every operand resolves,
     every label resolves, every def precedes its use.

  4. THE LAW (fail-closed) — a proven claim discharges when its site
     re-proves or when the optimiser eliminated the site (folded to a
     constant / dead code — no runtime operation remains). A proven
     claim whose site SURVIVES but no longer re-proves is a LOST-PROOF:
     the verdict fails, exit 1. A `safe_overflow` annotation neither an
     interval proof nor a pinned algebraic identity certifies is a
     FORGED ANNOTATION: exit 1. A dangling operand, a dangling label,
     an unreachable block: BROKEN-IR, exit 1.

Exit contract: 0 REPLAY-OK (every claim discharged, every annotation
certified, the transformed IR intact); 1 REPLAY-FAILED or a compile
refusal; 2 usage errors. The tool is read-only end to end (it writes
only when --out names a report directory). --json prints the machine
report (schema hls-reverify-report/v1, deterministic over an unchanged
tree — canonical JSON, sorted keys, no whitespace).
"""
from __future__ import annotations

import argparse
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOL_DIR)
for _p in (REPO_ROOT, TOOL_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hlreverify_parts import hlrev_common as com           # noqa: E402
from hlreverify_parts import hlrev_collect as coll         # noqa: E402
from hlreverify_parts import hlrev_ircheck as ircheck      # noqa: E402
from hlreverify_parts import hlrev_replay as replay        # noqa: E402


class CliError(Exception):
    """A refusal the CLI reports on stderr with exit 1."""


def _load_and_check(path):
    """The ONE authoritative pipeline: load_program + check — the same
    fixpoint every tool in this repository runs first."""
    from boot.boot import load_program
    from boot.checker import check
    from boot.lexer import HLError
    try:
        program = load_program(path)
    except HLError as ex:
        raise CliError("compile error: %s" % ex)
    except OSError:
        raise CliError("error: cannot open file %s" % path)
    try:
        check(program)
    except HLError as ex:
        raise CliError("compile error: %s" % ex)
    return program


def _build_modules(program, lto):
    """IR0 (pre-optimisation) and IR1 (optimised, fast=True)."""
    from ir import build_module
    from ir.optimize import optimize
    from boot.lexer import HLError
    try:
        mod0 = build_module(program)
        mod1 = build_module(program)
    except HLError as ex:
        raise CliError(
            "compile error: the HLIR layer cannot represent this "
            "program: %s" % ex)
    optimize(mod1, fast=True, lto=lto)
    return mod0, mod1


def _analyze(program, mod1):
    """The replay over every function of the transformed IR."""
    analyses = {}
    for fname, irf in mod1.functions.items():
        ast_fn = program["fns"].get(fname)
        entry_state = ircheck.seed_function(ast_fn) if ast_fn else None
        analyses[fname] = ircheck.analyze_function(irf, entry_state)
    return analyses


def run_reverify(path, lto=False, json_out=False, out_dir=None):
    program = _load_and_check(path)
    baseline = coll.harvest_baseline(program)
    mod0, mod1 = _build_modules(program, lto)
    analyses = _analyze(program, mod1)
    report = replay.reconcile(path, baseline, mod0, mod1, analyses, lto)

    if json_out:
        sys.stdout.write(com.canonical_json(report) + "\n")
    elif not out_dir:
        sys.stdout.write(replay.format_text(report) + "\n")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        dest = os.path.join(out_dir,
                            "hls-reverify-%s.report.json" % base)
        tmp = dest + ".tmp"
        with open(tmp, "w") as f:
            f.write(com.canonical_json(report) + "\n")
        os.replace(tmp, dest)
        if not json_out:
            sys.stdout.write("report: %s\n" % dest)
            sys.stdout.write(replay.format_text(report) + "\n")
    return 0 if report["verdict"] == "REPLAY-OK" else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "selftest":
        from hlreverify_parts import hlrev_selftest
        return hlrev_selftest.run()
    parser = argparse.ArgumentParser(
        prog="hls-reverify",
        add_help=False,
        description="memory-safety re-verification under -O fast "
                    "(proof replay)")
    parser.add_argument("file", nargs="?", help="the entry .hls file")
    parser.add_argument("--lto", action="store_true",
                        help="raise the cross-crate inline threshold "
                             "(the --lto build)")
    parser.add_argument("--json", action="store_true",
                        help="print the machine report")
    parser.add_argument("--out", metavar="DIR",
                        help="also write hls-reverify-<entry>"
                             ".report.json into DIR")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args(argv)
    if args.help:
        parser.print_help()
        return 0
    if not args.file:
        sys.stderr.write("usage: hls-reverify FILE [--lto] [--json] "
                         "[--out DIR]\n"
                         "       hls-reverify selftest\n")
        return 2
    try:
        return run_reverify(args.file, lto=args.lto, json_out=args.json,
                            out_dir=args.out)
    except CliError as ex:
        sys.stderr.write(str(ex) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
