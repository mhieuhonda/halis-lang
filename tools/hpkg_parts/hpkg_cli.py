"""cli - verbatim segment of the original tools/hls-pkg.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hpkg_cmds import (
    cmd_add, cmd_audit, cmd_build, cmd_init, cmd_lock, cmd_log, cmd_publish, cmd_verify,
)
from hpkg_common import (
    argparse, sys,
)

def main():
    parser = argparse.ArgumentParser(
        prog="hls-pkg",
        description="Halis package manager (Stage 13 release).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create a new package skeleton.")
    p_init.add_argument("name")
    p_init.set_defaults(func=cmd_init)

    p_add = sub.add_parser("add", help="Add a git dependency.")
    p_add.add_argument("name")
    p_add.add_argument("git")
    p_add.add_argument("path")
    p_add.add_argument("--tag", default=None)
    p_add.add_argument("--branch", default=None)
    p_add.set_defaults(func=cmd_add)

    p_lock = sub.add_parser("lock", help="Resolve dependencies and write hls-pkg.lock.")
    # Stage 22 (v0.41.0-alpha): --target <triple> — stamp the lockfile
    # with the cross-compilation target. The lockfile's `target` field
    # records which target this lockfile was generated for; `hls-pkg
    # verify` checks that the lockfile's target matches the build's
    # target (a mismatch means the dependencies were resolved for a
    # different platform — re-lock for the current target).
    p_lock.add_argument("--target", default=None,
                        help="stamp the lockfile with this target triple "
                             "(e.g. aarch64-apple-darwin). When omitted, "
                             "the lockfile is target-agnostic (target: null).")
    p_lock.set_defaults(func=cmd_lock)

    p_audit = sub.add_parser("audit", help="Print the total effect report of the dep tree.")
    p_audit.set_defaults(func=cmd_audit)

    p_verify = sub.add_parser("verify", help="Verify lockfile SHA-256 hashes + commit + log.")
    # Stage 22: --target <triple> — verify that the lockfile was
    # generated for this target (mismatch = the deps were resolved for
    # a different platform).
    p_verify.add_argument("--target", default=None,
                          help="verify that the lockfile's target matches "
                               "this triple (mismatch = re-lock for the "
                               "current target)")
    p_verify.set_defaults(func=cmd_verify)

    p_build = sub.add_parser("build", help="Compile the package with resolved dependencies.")
    p_build.add_argument("--entry", default="main.hls")
    # Stage 20 (v0.36.0-alpha): whole-program LTO build (cross-crate
    # inlining + dead-code elimination via hlc --lto).
    p_build.add_argument("--lto", action="store_true",
                         help="compile natively with --lto (cross-crate "
                              "inlining + whole-program DCE)")
    # Stage 22 (v0.41.0-alpha): --target <triple> — cross-compile the
    # package's entry point to a foreign binary via hlcross.
    p_build.add_argument("--target", default=None,
                         help="cross-compile to this target triple "
                              "(e.g. aarch64-apple-darwin, wasm32-unknown-unknown). "
                              "When omitted, builds for the host.")
    # Stage 24 (v0.43.0-alpha): wasm-specific build flags (only used
    # when --target starts with wasm32). The default (--wasm-opt auto,
    # --opt-level O3, --glue compact) matches the Stage 24 acceptance
    # criterion ("hls-pkg build --target wasm32 runs wasm-opt -O3").
    p_build.add_argument("--wasm-opt", default="auto",
                         choices=["auto", "on", "off"],
                         help="Stage 24: wasm size optimizer mode "
                              "(default: auto = run in-tree + external)")
    p_build.add_argument("--opt-level", default="O3",
                         choices=["O1", "O2", "O3", "Os"],
                         help="Stage 24: optimization level (default: O3)")
    p_build.add_argument("--glue", default="compact",
                         choices=["compact", "verbose"],
                         help="Stage 24: JS glue style (default: compact)")
    p_build.set_defaults(func=cmd_build)

    # Stage 13 release: transparency log commands.
    p_publish = sub.add_parser("publish",
                              help="Append the package to the transparency log.")
    p_publish.set_defaults(func=cmd_publish)

    p_log = sub.add_parser("log",
                          help="Print or verify the transparency log.")
    p_log.add_argument("--verify", action="store_true",
                       help="Verify the chain hashes of the log.")
    p_log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    # BUG-DS4-25: manifest/lockfile problems used to escape as raw
    # Python tracebacks. Report them as clean CLI errors.
    # SCAN-B fix: broaden the caught types — AttributeError and TypeError
    # are raised by the malformed-shape paths and previously escaped.
    try:
        return args.func(args)
    except (ValueError, KeyError, AttributeError, TypeError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 1
    except FileNotFoundError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 1
    except (OSError, IOError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 1




__all__ = [
    "main",
]
