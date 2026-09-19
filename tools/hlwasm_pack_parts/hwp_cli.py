"""hwp_cli — argument parser + subcommand orchestrator (Stage 76).

Subcommands (the wasm-pack surface, adapted for Halis):

  new NAME            scaffold a new Halis web-library project
                      (main.hls + hls.pack.toml + README stub).
  build INPUT         compile INPUT.hls -> publish-ready pkg/ dir
                      (--out, --target, --name, --version, --scope,
                      --wasm-opt, --opt-level, --config).
  pack                tar a built pkg/ into <stem>-<version>.tgz.
  publish             dry-run (default) or real ``npm publish``.
  check PKG           validate a built pkg/ dir (no execution).
  test PKG            node smoke-run (SKIP when node is absent).

Global flags: --config PATH (explicit config file), --color/--no-color,
--version. A ``hls.pack.toml`` (or ``hls.pack.json``) in the cwd is
auto-detected; CLI flags always override it (the Stage 75 convention).
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from hwp_common import (DEFAULT_OUT_DIR, PACK_BANNER, PACK_TARGETS, log_error, log_info)


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("_", "-")


TEMPLATE_MAIN = """# {name} — Halis web library (hls-wasm-pack Stage 76)
#
# Build a publish-ready npm package with:
#   python3 tools/hlwasm_pack.py build {stem}.hls --out pkg --target bundler
#
# NOTE ON THE SUBSET: the freestanding wasm emitter lowers let / if /
# while / for-range / calls / literals — struct literals, match, and
# method calls land with the HLIR emitter, so this template stays
# inside the supported subset.

import "std.jsffi"

fn {fname}_add(a: int, b: int) -> int {{
    return a + b
}}

fn {fname}_greet(name: str) -> str {{
    return "Hello, " + name + "!"
}}

fn jsffi_on_callback(cb_id: int, arg: str) -> str uses IO {{
    if cb_id == 1 {{
        return {fname}_greet(arg)
    }}
    return ""
}}

fn main() -> int uses IO {{
    println("{name} ready (" + {fname}_add(40, 2).to_str() + ")")
    js_console_log({fname}_greet("wasm-pack"))
    return 0
}}
"""

TEMPLATE_TOML = """# hls.pack.toml — hls-wasm-pack project config (Stage 76).
# CLI flags always override these values.
[package]
name = "{name}"
version = "0.1.0"
description = "{name} — a Halis WebAssembly library"
license = "MIT"
target = "bundler"
out = "pkg"
wasm_opt = "auto"
opt_level = "O3"
"""


def cmd_new(name: str, scope: Optional[str] = None,
            out_base: Optional[str] = None,
            template: str = "minimal") -> int:
    """Scaffold a new project; return the exit code."""
    from hwp_manifest import is_valid_name
    full = ("%s/%s" % (scope, name)) if scope else name
    if scope and not scope.startswith("@"):
        scope = "@" + scope
        full = "%s/%s" % (scope, name)
    if not is_valid_name(full):
        log_error("invalid package name %r" % full)
        return 2
    dest = out_base or _slug(name.rsplit("/", 1)[-1])
    if os.path.exists(dest):
        log_error("destination already exists: %r" % dest)
        return 1
    os.makedirs(dest)
    stem = _slug(name.rsplit("/", 1)[-1])
    fname = "".join(ch if ch.isalnum() else "_" for ch in stem)
    main_src = TEMPLATE_MAIN.format(name=full, stem=stem, fname=fname or "app")
    with open(os.path.join(dest, stem + ".hls"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(main_src)
    with open(os.path.join(dest, "hls.pack.toml"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(TEMPLATE_TOML.format(name=full))
    with open(os.path.join(dest, "README.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("# %s\n\nA Halis WebAssembly library. "
                "See `hls.pack.toml` for pack settings.\n" % full)
    log_info("created %s/ (%s.hls + hls.pack.toml + README.md)"
             % (dest, stem))
    return 0


def _resolve_opts(args: Any) -> Dict[str, Any]:
    from hwp_manifest import load_config, merge_config
    cfg = load_config(getattr(args, "config", None))
    opts = merge_config(cfg, args)
    scope = opts.get("scope")
    name = opts.get("name")
    if scope and name and "/" not in str(name):
        s = str(scope)
        if not s.startswith("@"):
            s = "@" + s
        opts["name"] = "%s/%s" % (s, name)
    return opts


def cmd_build(args: Any) -> int:
    from hwp_build import build
    opts = _resolve_opts(args)
    if not opts.get("name") and getattr(args, "input", None):
        base = os.path.splitext(os.path.basename(args.input))[0]
        opts["name"] = _slug(base) or "halis-app"
    out_dir = str(opts.get("out", DEFAULT_OUT_DIR))
    try:
        build(args.input, out_dir, opts)
    except ValueError as ex:
        log_error(str(ex))
        return 1
    except Exception as ex:  # noqa: BLE001 — clean CLI error, like hlwasm
        log_error("build failed: %s" % ex)
        return 1
    return 0


def cmd_pack(args: Any) -> int:
    from hwp_pack import pack
    pkg_dir = getattr(args, "pkg", None) or DEFAULT_OUT_DIR
    try:
        pack(pkg_dir, getattr(args, "tgz", None))
    except ValueError as ex:
        log_error(str(ex))
        return 1
    return 0


def cmd_publish(args: Any) -> int:
    from hwp_publish import publish
    try:
        result = publish(getattr(args, "tarball"),
                         registry=getattr(args, "registry", None),
                         access=getattr(args, "access", "public") or "public",
                         tag=getattr(args, "tag", "latest") or "latest",
                         dry_run=not getattr(args, "no_dry_run", False))
    except ValueError as ex:
        log_error(str(ex))
        return 1
    if result.get("published"):
        return 0
    if result.get("dry_run"):
        return 0
    return 1


def cmd_check(args: Any) -> int:
    from hwp_validate import check
    ok, msgs = check(getattr(args, "pkg"))
    for m in msgs:
        print(("  [%s] %s" % ("OK" if m.startswith("OK:") else
                              ("FAIL" if m.startswith("FAIL:") else "INFO"),
                              m)))
    print("CHECK %s: %s" % ("PASSED" if ok else "FAILED",
                            getattr(args, "pkg")))
    return 0 if ok else 1


def cmd_test(args: Any) -> int:
    from hwp_validate import test_pkg
    status, detail = test_pkg(getattr(args, "pkg"))
    print("TEST %s: %s (%s)" % (status.upper(), getattr(args, "pkg"), detail))
    return 0 if status in ("ok", "skip") else 1


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hlwasm_pack.py",
        description="Stage 76 hls-wasm-pack: publish-ready wasm + JS glue "
                    "(the wasm-pack equivalent for Halis).")
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="explicit config file (default: auto-detect "
                         "hls.pack.toml / hls.pack.json in cwd)")
    ap.add_argument("--color", action="store_true",
                    help="force ANSI colors in log output")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI colors")
    ap.add_argument("--version", action="store_true",
                    help="print the banner and exit")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    p_new = sub.add_parser("new", help="scaffold a new Halis web library")
    p_new.add_argument("name", help="package name (or bare name with --scope)")
    p_new.add_argument("--scope", default=None,
                       help="npm scope (e.g. @myorg)")
    p_new.add_argument("--dir", default=None,
                       help="destination directory (default: slug of name)")
    p_new.add_argument("--template", default="minimal",
                       choices=["minimal", "jsffi", "counter"],
                       help="scaffold template (all three currently emit "
                            "the minimal wasm-subset app)")

    p_build = sub.add_parser("build", help="compile INPUT.hls -> pkg/ dir")
    p_build.add_argument("input", help="HLS source file")
    p_build.add_argument("--out", default=None,
                         help="output dir (default: pkg)")
    p_build.add_argument("--target", default=None,
                         choices=list(PACK_TARGETS),
                         help="pack target (default: bundler)")
    p_build.add_argument("--name", default=None, help="package name")
    p_build.add_argument("--scope", default=None, help="npm scope")
    p_build.add_argument("--version", default=None, dest="version",
                         help="package version (semver X.Y.Z[-pre])")
    p_build.add_argument("--description", default=None)
    p_build.add_argument("--license", default=None)
    p_build.add_argument("--repository", default=None)
    p_build.add_argument("--authors", default=None, nargs="*")
    p_build.add_argument("--keywords", default=None, nargs="*")
    p_build.add_argument("--wasm-opt", default=None,
                         choices=["auto", "on", "off"])
    p_build.add_argument("--opt-level", default=None,
                         choices=["O1", "O2", "O3", "Os"])

    p_pack = sub.add_parser("pack", help="tar a built pkg/ into a .tgz")
    p_pack.add_argument("--pkg", default=None, help="built pkg dir")
    p_pack.add_argument("--tgz", default=None, help="output tarball path")

    p_pub = sub.add_parser("publish", help="publish a .tgz (dry-run default)")
    p_pub.add_argument("tarball", help="tarball from 'pack'")
    p_pub.add_argument("--registry", default=None)
    p_pub.add_argument("--access", default="public",
                       choices=["public", "restricted"])
    p_pub.add_argument("--tag", default="latest")
    p_pub.add_argument("--no-dry-run", action="store_true",
                       help="actually run 'npm publish' (default: dry-run)")

    p_check = sub.add_parser("check", help="validate a built pkg/ dir")
    p_check.add_argument("pkg", help="built pkg dir")

    p_test = sub.add_parser("test", help="node smoke-run a built pkg/ dir")
    p_test.add_argument("pkg", help="built pkg dir")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    cmd = getattr(args, "cmd", None)
    if getattr(args, "version", False) is True and cmd is None:
        print(PACK_BANNER)
        return 0
    if cmd == "new":
        return cmd_new(args.name, scope=getattr(args, "scope", None),
                       out_base=getattr(args, "dir", None),
                       template=getattr(args, "template", "minimal"))
    if cmd == "build":
        return cmd_build(args)
    if cmd == "pack":
        return cmd_pack(args)
    if cmd == "publish":
        return cmd_publish(args)
    if cmd == "check":
        return cmd_check(args)
    if cmd == "test":
        return cmd_test(args)
    ap.print_help()
    return 2


__all__ = [
    "build_arg_parser",
    "cmd_build",
    "cmd_check",
    "cmd_new",
    "cmd_pack",
    "cmd_publish",
    "cmd_test",
    "main",
]
