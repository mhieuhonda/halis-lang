"""hwp_manifest — package manifest handling for Stage 76.

Reads the optional ``hls.pack.toml`` / ``hls.pack.json`` project config,
validates npm package names + semver versions, and writes the
publish-ready ``package.json``.

Name rules (npm, enforced):
  * ``name`` or ``@scope/name``; lowercase; no spaces; ``[a-z0-9._~-]``
    plus at most one ``/`` after a leading ``@scope``.
  * Rejects ``..``, leading ``-``/``.``, empty segments, ``node_modules``,
    and anything that escapes the output dir (path-traversal defence).

Version rules: strict ``X.Y.Z`` with optional ``-prerelease`` suffix
(the Halis ``v0.95.0-alpha`` style maps to ``0.95.0-alpha`` in npm).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._~-]*$")
_SCOPE_RE = re.compile(r"^@[a-z0-9][a-z0-9._~-]*$")
_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")

CONFIG_TOML_NAMES = ("hls.pack.toml",)
CONFIG_JSON_NAMES = ("hls.pack.json",)


def is_valid_name(name: str) -> bool:
    """Return True iff ``name`` is a publishable npm package name."""
    if not name or len(name) > 214:
        return False
    if name in ("node_modules", "favicon.ico"):
        return False
    if ".." in name or "\\" in name:
        return False
    if name.startswith((".", "-", "_")):
        return False
    if name.startswith("@"):
        # Scoped: @scope/name — exactly one slash, both parts valid.
        if name.count("/") != 1:
            return False
        scope, _, rest = name.partition("/")
        if not rest or rest.startswith((".", "-", "_")):
            return False
        return bool(_SCOPE_RE.match(scope)) and bool(_NAME_RE.match(rest))
    return bool(_NAME_RE.match(name))


def validate_name(name: str) -> str:
    if not is_valid_name(name):
        raise ValueError(
            "invalid package name %r: use lowercase 'name' or "
            "'@scope/name' ([a-z0-9._~-], no spaces, no '..')" % (name,))
    return name


def is_valid_version(version: str) -> bool:
    return bool(_VERSION_RE.match(version or ""))


def validate_version(version: str) -> str:
    if not is_valid_version(version):
        raise ValueError(
            "invalid version %r: use semver 'X.Y.Z' with optional "
            "'-prerelease' (e.g. '0.95.0-alpha')" % (version,))
    return version


def file_stem(name: str) -> str:
    """Map a package name to a file stem: ``@scope/pkg`` -> ``pkg``."""
    stem = name.rsplit("/", 1)[-1].lstrip("@")
    return stem or "halis-pkg"


def load_config(path: Optional[str] = None,
                 cwd: Optional[str] = None) -> Dict[str, Any]:
    """Load ``hls.pack.toml`` (or ``.json``) config; {} when absent.

    Explicit ``path`` wins; otherwise auto-detect in ``cwd`` (or the
    process cwd). TOML needs Python 3.11+ ``tomllib``; on older Pythons
    only the JSON shape is read (TOML falls back to {} with no error —
    CLI flags still work, mirroring the Stage 75 ``hlserve_config``
    behaviour).
    """
    base = cwd or os.getcwd()
    candidates: List[str] = []
    if path:
        candidates = [path]
    else:
        candidates = ([os.path.join(base, n) for n in CONFIG_TOML_NAMES]
                      + [os.path.join(base, n) for n in CONFIG_JSON_NAMES])
    for cand in candidates:
        if not os.path.isfile(cand):
            continue
        if cand.endswith(".json"):
            with open(cand, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _normalize_config(data)
        # TOML shape.
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            return {}
        with open(cand, "rb") as f:
            data = tomllib.load(f)
        return _normalize_config(data)
    return {}


def _normalize_config(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    # Accept both a flat table and a nested [package] table.
    cfg: Dict[str, Any] = {}
    pkg = data.get("package")
    if isinstance(pkg, dict):
        cfg.update(pkg)
    for key in ("name", "version", "description", "scope", "target",
                "out", "license", "repository", "authors", "keywords",
                "wasm_opt", "opt_level", "registry", "access", "tag"):
        if key in data and key not in cfg:
            cfg[key] = data[key]
    return cfg


def merge_config(cfg: Dict[str, Any], args: Any) -> Dict[str, Any]:
    """Overlay parsed CLI args on top of the file config (CLI wins)."""
    merged: Dict[str, Any] = dict(cfg)
    for key in ("name", "version", "description", "scope", "target",
                "out", "license", "repository", "wasm_opt", "opt_level",
                "registry", "access", "tag"):
        val = getattr(args, key, None)
        if val is not None:
            merged[key] = val
    authors = getattr(args, "authors", None)
    if authors:
        merged["authors"] = authors
    keywords = getattr(args, "keywords", None)
    if keywords:
        merged["keywords"] = keywords
    return merged


def build_package_json(opts: Dict[str, Any], js_file: str, dts_file: str,
                       wasm_file: str) -> Dict[str, Any]:
    """Build the ``package.json`` dict for a packed ``pkg/`` dir."""
    name = validate_name(str(opts.get("name", "halis-app")))
    version = validate_version(str(opts.get("version", "0.1.0")))
    target = str(opts.get("target", "bundler"))
    description = str(opts.get("description", "WebAssembly package built with hls-wasm-pack"))
    license_ = str(opts.get("license", "MIT"))
    authors = opts.get("authors", [])
    keywords = opts.get("keywords", ["halis", "wasm"])
    repository = opts.get("repository")
    if isinstance(authors, str):
        authors = [authors]
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    pkg: Dict[str, Any] = {
        "name": name,
        "version": version,
        "description": description,
        "license": license_,
        "type": "module" if target in ("bundler", "web", "deno") else "commonjs",
        "main": js_file,
        "types": dts_file,
        "files": [wasm_file, js_file, dts_file, "README.md",
                  ".pack-manifest.json"],
        "sideEffects": False,
        "keywords": keywords,
        "halis": {
            "packVersion": 1,
            "target": target,
            "wasm": wasm_file,
            "js": js_file,
            "dts": dts_file,
            "toolchain": "hls-wasm-pack v0.95.0-alpha (Stage 76)",
        },
    }
    if target in ("bundler", "web", "deno"):
        pkg["module"] = js_file
        pkg["exports"] = {".": {"types": "./" + dts_file,
                                "import": "./" + js_file}}
    elif target == "nodejs":
        # Deep-scan-30 fix: the nodejs branch used to sit INSIDE the
        # `target in ("bundler", "web", "deno")` guard — unreachable, so
        # nodejs packages shipped no `exports` map at all.
        pkg["exports"] = {".": {"types": "./" + dts_file,
                                "require": "./" + js_file}}
    if authors:
        pkg["author"] = authors[0] if len(authors) == 1 else authors
    if repository:
        pkg["repository"] = repository
    return pkg


def write_package_json(out_dir: str, pkg: Dict[str, Any]) -> str:
    path = os.path.join(out_dir, "package.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")
    return path


__all__ = [
    "CONFIG_JSON_NAMES",
    "CONFIG_TOML_NAMES",
    "build_package_json",
    "file_stem",
    "is_valid_name",
    "is_valid_version",
    "load_config",
    "merge_config",
    "validate_name",
    "validate_version",
    "write_package_json",
]
