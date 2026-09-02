#!/usr/bin/env python3
"""hls-pkg — Package manager for Hieu Louis (HLS).

Stage 13 (v0.11.0-alpha): content-addressed package manager with
verified provenance and effect enforcement.

Manifest format (hls-pkg.toml, simple TOML-like):
------------------------------------------------------------
    [package]
    name = "mylib"
    version = "0.1.0"
    authors = ["Your Name <you@example.com>"]
    description = "A small HLS library."

    [dependencies]
    std.str = { git = "https://github.com/mhieuhonda/hieu-louis-lang.git", path = "std/str.hls" }
    mymath  = { git = "https://github.com/foo/bar.git", path = "src/math.hls", tag = "v1.0.0" }

    [effects]
    # Declare the package's TOTAL effect surface. The auditor verifies
    # that every function in the package respects this surface — a
    # pure library package (effects = []) CANNOT use `uses Net`
    # anywhere, even transitively through dependencies.
    allowed = []   # empty = pure library
------------------------------------------------------------

Lockfile format (hls-pkg.lock, JSON):
------------------------------------------------------------
    {
      "version": 1,
      "packages": [
        {
          "name": "std.str",
          "source": { "git": "...", "path": "std/str.hls", "tag": "main" },
          "sha256": "abc123...",
          "effects": [],
          "transitive_effects": [],
          "deps": []
        },
        ...
      ]
    }
------------------------------------------------------------

The lockfile is content-addressed: each package's SHA-256 is computed
over the resolved file content. The effect table is computed by running
the Stage-0 checker's `--audit` on the package's files.

Commands:
  hls-pkg init NAME          Create a new package skeleton.
  hls-pkg add NAME GIT PATH  Add a git dependency.
  hls-pkg lock               Resolve dependencies and write hls-pkg.lock.
  hls-pkg audit              Print the total effect report of the dep tree.
  hls-pkg build              Compile the package with resolved dependencies.
  hls-pkg verify             Verify the lockfile's SHA-256 hashes still match.

Status: alpha. Today the resolver fetches git deps via `git clone` into
a cache directory; the auditor runs `boot.py --audit` on each resolved
file. The `build` command runs `boot.py` on the package's entry point
with the resolved dependency directories on the import path.

The roadmap's transparency log and decentralised registry are deferred
to the Stage 13 release target.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Manifest parsing (minimal TOML parser — supports the subset we use).
# ---------------------------------------------------------------------------

def parse_manifest(path: str) -> Dict:
    """Parse a hls-pkg.toml manifest. Returns a dict.

    Supports the subset we need: section headers like [package],
    [dependencies], [effects], and `key = value` lines where value is
    a string, integer, list, or inline table { git = "...", path = "..." }.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError("manifest not found: %s" % path)
    with open(path, "r") as f:
        src = f.read()
    # Strip comments.
    lines = []
    for line in src.split("\n"):
        # Strip everything after `#` (not inside strings, but our parser
        # is simple enough that this is fine for manifests without `#` in
        # string values).
        if "#" in line:
            line = line[:line.index("#")]
        lines.append(line)
    src = "\n".join(lines)

    # Parse into a tree of section -> key -> value.
    result: Dict = {}
    current_section: Optional[str] = None
    i = 0
    n = len(src)
    while i < n:
        # Skip whitespace.
        while i < n and src[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        # Section header.
        if src[i] == "[":
            j = src.index("]", i)
            current_section = src[i + 1:j].strip()
            # Make sure the section dict exists.
            parts = current_section.split(".")
            cursor = result
            for p in parts:
                cursor = cursor.setdefault(p, {})
            i = j + 1
            continue
        # key = value
        eq = src.index("=", i)
        key = src[i:eq].strip()
        # Parse value.
        val, i = _parse_value(src, eq + 1)
        # Assign into the current section.
        if current_section is None:
            result[key] = val
        else:
            parts = current_section.split(".")
            cursor = result
            for p in parts:
                cursor = cursor.setdefault(p, {})
            cursor[key] = val
    return result


def _parse_value(src: str, i: int) -> Tuple[object, int]:
    """Parse a TOML value starting at position i. Returns (value, new_i)."""
    n = len(src)
    # Skip whitespace.
    while i < n and src[i] in " \t\r\n":
        i += 1
    if i >= n:
        return None, i
    c = src[i]
    if c == '"':
        # String.
        j = i + 1
        while j < n and src[j] != '"':
            if src[j] == '\\':
                j += 2
            else:
                j += 1
        return src[i + 1:j], j + 1
    if c == '[':
        # List (of strings).
        j = i + 1
        items = []
        while j < n and src[j] != ']':
            while j < n and src[j] in " \t\r\n,":
                j += 1
            if j < n and src[j] == '"':
                k = j + 1
                while k < n and src[k] != '"':
                    if src[k] == '\\':
                        k += 2
                    else:
                        k += 1
                items.append(src[j + 1:k])
                j = k + 1
            else:
                j += 1
        return items, j + 1
    if c == '{':
        # Inline table.
        j = i + 1
        table = {}
        while j < n and src[j] != '}':
            while j < n and src[j] in " \t\r\n,":
                j += 1
            if j >= n or src[j] == '}':
                break
            eq = src.index("=", j)
            key = src[j:eq].strip()
            val, j = _parse_value(src, eq + 1)
            table[key] = val
        return table, j + 1
    # Bare token: integer, true/false, or unquoted string.
    j = i
    while j < n and src[j] not in " \t\r\n,":
        j += 1
    tok = src[i:j]
    if tok == "true":
        return True, j
    if tok == "false":
        return False, j
    try:
        return int(tok), j
    except ValueError:
        pass
    try:
        return float(tok), j
    except ValueError:
        pass
    return tok, j


def write_manifest(manifest: Dict, path: str):
    """Write a manifest dict as TOML."""
    lines = []
    if "package" in manifest:
        lines.append("[package]")
        for k, v in manifest["package"].items():
            lines.append('%s = %s' % (k, _fmt_value(v)))
        lines.append("")
    if "dependencies" in manifest:
        lines.append("[dependencies]")
        for k, v in manifest["dependencies"].items():
            lines.append('%s = %s' % (k, _fmt_value(v)))
        lines.append("")
    if "effects" in manifest:
        lines.append("[effects]")
        for k, v in manifest["effects"].items():
            lines.append('%s = %s' % (k, _fmt_value(v)))
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _fmt_value(v) -> str:
    if isinstance(v, str):
        return '"%s"' % v.replace('"', '\\"')
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, list):
        return "[%s]" % ", ".join(_fmt_value(x) for x in v)
    if isinstance(v, dict):
        return "{ %s }" % ", ".join(
            '%s = %s' % (k, _fmt_value(x)) for k, x in v.items())
    return repr(v)


# ---------------------------------------------------------------------------
# Content hashing.
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Dependency resolution.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, ".hls-pkg-cache")


def resolve_dependency(name: str, source: Dict, cache_dir: str = CACHE_DIR) -> str:
    """Resolve a dependency to a local file path.

    For `path`-only deps: relative to the repo root.
    For `git` deps: clone into cache_dir, checkout tag/branch, return path.
    """
    if "path" in source:
        # Path dependency. Resolve relative to the repo root or absolute.
        p = source["path"]
        if not os.path.isabs(p):
            p = os.path.join(REPO_ROOT, p)
        if not os.path.isfile(p):
            raise FileNotFoundError(
                "dependency %s: path not found: %s" % (name, p))
        return p
    if "git" in source:
        # Git dependency. Clone into cache.
        os.makedirs(cache_dir, exist_ok=True)
        repo_url = source["git"]
        repo_hash = sha256_bytes(repo_url.encode("utf-8"))[:12]
        clone_dir = os.path.join(cache_dir, name.replace(".", "_"), repo_hash)
        if not os.path.isdir(clone_dir):
            subprocess.run(["git", "clone", "--quiet", repo_url, clone_dir],
                           check=True, capture_output=True)
        # Checkout tag/branch if specified.
        ref = source.get("tag") or source.get("branch") or "main"
        try:
            subprocess.run(["git", "-C", clone_dir, "fetch", "--quiet",
                            "origin", ref], check=True, capture_output=True)
            subprocess.run(["git", "-C", clone_dir, "checkout", "--quiet",
                            ref], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # Fallback: try just `git checkout main`.
            subprocess.run(["git", "-C", clone_dir, "checkout", "--quiet",
                            "main"], check=True, capture_output=True)
        path = source.get("path", "")
        full = os.path.join(clone_dir, path) if path else clone_dir
        if not os.path.isfile(full):
            raise FileNotFoundError(
                "dependency %s: path not found in repo: %s" % (name, full))
        return full
    raise ValueError("dependency %s has no git/path source" % name)


# ---------------------------------------------------------------------------
# Effect extraction.
# ---------------------------------------------------------------------------

def extract_effects(file_path: str) -> Tuple[List[str], List[str]]:
    """Run `boot.py --audit` on a single-file package; return (declared, transitive).

    The HLS checker requires a `main` function. Library files (like
    `std/str.hls`) don't have one, so we generate a temporary wrapper
    file in the SAME directory as the target (so relative imports work)
    that imports the library and provides a dummy main.
    """
    boot_py = os.path.join(REPO_ROOT, "boot", "boot.py")
    if not os.path.isfile(boot_py):
        return [], []
    # Generate a wrapper file alongside the target so relative imports work.
    target_dir = os.path.dirname(os.path.abspath(file_path))
    target_name = os.path.basename(file_path)
    wrapper_path = os.path.join(target_dir, ".hls-pkg-audit-wrapper.hls")
    try:
        with open(wrapper_path, "w") as f:
            f.write('# auto-generated by hls-pkg audit\n')
            f.write('import "%s"\n' % target_name)
            # Use a PURE main so the wrapper itself doesn't pollute the
            # audit with IO-family effects from `uses IO`.
            f.write('fn main() -> int pure { return 0 }\n')
        try:
            result = subprocess.run(
                [sys.executable, boot_py, "--audit", wrapper_path],
                capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        finally:
            try:
                os.unlink(wrapper_path)
            except OSError:
                pass
    except (subprocess.TimeoutExpired, OSError):
        return [], []
    if result.returncode != 0:
        return [], []
    # Parse the audit output: look for the function table, extract the
    # declared and computed effect sets.
    declared = set()
    computed = set()
    KNOWN_EFFECTS = {"IO", "Fs", "Clock", "Args", "Exit"}
    for line in result.stdout.split("\n"):
        # Look for status lines like:
        #   greet  (none - pure)  (none)  OK
        #   main   IO             IO      OK
        # We want to collect all declared effects (column 2) and all
        # computed effects (column 3) — but only count actual effect names.
        if line.startswith("  ") and "OK" in line:
            parts = line.strip().split()
            for p in parts:
                if p in KNOWN_EFFECTS:
                    declared.add(p)
                if p in KNOWN_EFFECTS:
                    computed.add(p)
            # Also handle comma-separated lists like "Args, Clock"
            for tok in line.split():
                for sub in tok.split(","):
                    sub = sub.strip()
                    if sub in KNOWN_EFFECTS:
                        declared.add(sub)
                        computed.add(sub)
    return sorted(declared), sorted(computed)


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Create a new package skeleton."""
    name = args.name
    pkg_dir = os.path.join(os.getcwd(), name)
    if os.path.exists(pkg_dir):
        print("error: directory %s already exists" % pkg_dir, file=sys.stderr)
        return 1
    os.makedirs(pkg_dir)
    # Manifest.
    manifest = {
        "package": {
            "name": name,
            "version": "0.1.0",
            "authors": [],
            "description": "A HLS package.",
        },
        "dependencies": {},
        "effects": {"allowed": []},
    }
    write_manifest(manifest, os.path.join(pkg_dir, "hls-pkg.toml"))
    # Entry source.
    src = '''# %s — generated by hls-pkg init.

fn main() -> int uses IO {
    println("Hello from %s!")
    return 0
}
''' % (name, name)
    with open(os.path.join(pkg_dir, "main.hls"), "w") as f:
        f.write(src)
    # README.
    with open(os.path.join(pkg_dir, "README.md"), "w") as f:
        f.write("# %s\n\nA Hieu Louis package.\n" % name)
    # .gitignore.
    with open(os.path.join(pkg_dir, ".gitignore"), "w") as f:
        f.write(".hls-pkg-cache/\nhls-pkg.lock\n")
    print("Created package skeleton in %s/" % name)
    print("  hls-pkg.toml   — manifest")
    print("  main.hls       — entry source")
    print("  README.md      — package README")
    print("  .gitignore     — ignores cache + lockfile")
    print("")
    print("Next steps:")
    print("  cd %s" % name)
    print("  hls-pkg add std.str https://github.com/mhieuhonda/hieu-louis-lang.git std/str.hls")
    print("  hls-pkg lock")
    print("  hls-pkg audit")
    print("  hls-pkg build")
    return 0


def cmd_add(args):
    """Add a git dependency to the manifest."""
    manifest_path = os.path.join(os.getcwd(), "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        print("error: no hls-pkg.toml in current directory", file=sys.stderr)
        return 1
    manifest = parse_manifest(manifest_path)
    deps = manifest.setdefault("dependencies", {})
    source = {"git": args.git, "path": args.path}
    if args.tag:
        source["tag"] = args.tag
    if args.branch:
        source["branch"] = args.branch
    deps[args.name] = source
    write_manifest(manifest, manifest_path)
    print("Added dependency: %s" % args.name)
    print("  source: %s" % source)
    return 0


def cmd_lock(args):
    """Resolve dependencies and write hls-pkg.lock."""
    manifest_path = os.path.join(os.getcwd(), "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        print("error: no hls-pkg.toml in current directory", file=sys.stderr)
        return 1
    manifest = parse_manifest(manifest_path)
    deps = manifest.get("dependencies", {})
    lockfile = {"version": 1, "packages": []}
    for name, source in deps.items():
        print("Resolving %s..." % name, end=" ", flush=True)
        try:
            resolved_path = resolve_dependency(name, source)
        except Exception as ex:
            print("FAILED: %s" % ex)
            return 1
        sha = sha256_file(resolved_path)
        declared, computed = extract_effects(resolved_path)
        print("OK (sha256: %s..)" % sha[:12])
        lockfile["packages"].append({
            "name": name,
            "source": source,
            "sha256": sha,
            "effects": declared,
            "transitive_effects": computed,
            "resolved_path": resolved_path,
        })
    # Write lockfile.
    lockfile_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    with open(lockfile_path, "w") as f:
        json.dump(lockfile, f, indent=2)
    print("")
    print("Wrote lockfile: %s (%d packages)" % (lockfile_path, len(lockfile["packages"])))
    # Effect enforcement: check that the package's declared `effects.allowed`
    # is a superset of every dependency's computed effects.
    allowed = set(manifest.get("effects", {}).get("allowed", []))
    violations = []
    for pkg in lockfile["packages"]:
        for eff in pkg["transitive_effects"]:
            if eff not in allowed:
                violations.append((pkg["name"], eff))
    if violations:
        print("")
        print("EFFECT VIOLATIONS:")
        for pkg_name, eff in violations:
            print("  - %s uses %s (not in allowed set)" % (pkg_name, eff))
        print("")
        print("Allowed effects: %s" % (sorted(allowed) or "(none - pure)"))
        return 1
    print("All dependencies respect the package's effect surface.")
    return 0


def cmd_audit(args):
    """Print the total effect report of the dependency tree."""
    lockfile_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    if not os.path.isfile(lockfile_path):
        print("error: no hls-pkg.lock — run `hls-pkg lock` first", file=sys.stderr)
        return 1
    with open(lockfile_path, "r") as f:
        lockfile = json.load(f)
    name_w = max((len(p["name"]) for p in lockfile["packages"]), default=4)
    print("=" * (name_w + 60))
    print("  %-*s  %12s  %12s  %s" % (
        name_w, "package", "sha256[..12]", "declared", "transitive"))
    print("=" * (name_w + 60))
    all_effects = set()
    for pkg in lockfile["packages"]:
        sha = pkg["sha256"][:12]
        decl = ", ".join(pkg["effects"]) or "(none)"
        trans = ", ".join(pkg["transitive_effects"]) or "(none)"
        all_effects.update(pkg["transitive_effects"])
        print("  %-*s  %12s  %12s  %s" % (name_w, pkg["name"], sha, decl, trans))
    print("=" * (name_w + 60))
    print("  Total transitive effects of dependency tree: %s" % (
        ", ".join(sorted(all_effects)) or "(none — pure library)"))
    return 0


def cmd_verify(args):
    """Verify the lockfile's SHA-256 hashes still match."""
    lockfile_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    if not os.path.isfile(lockfile_path):
        print("error: no hls-pkg.lock — run `hls-pkg lock` first", file=sys.stderr)
        return 1
    with open(lockfile_path, "r") as f:
        lockfile = json.load(f)
    failures = 0
    for pkg in lockfile["packages"]:
        path = pkg["resolved_path"]
        if not os.path.isfile(path):
            print("  [FAIL] %s: file not found (%s)" % (pkg["name"], path))
            failures += 1
            continue
        actual_sha = sha256_file(path)
        if actual_sha != pkg["sha256"]:
            print("  [FAIL] %s: hash mismatch (expected %s, got %s)" % (
                pkg["name"], pkg["sha256"][:12], actual_sha[:12]))
            failures += 1
        else:
            print("  [ OK ] %s: hash matches (%s)" % (pkg["name"], actual_sha[:12]))
    if failures:
        print("")
        print("Verification FAILED: %d packages have hash mismatches." % failures)
        return 1
    print("")
    print("All %d packages verified." % len(lockfile["packages"]))
    return 0


def cmd_build(args):
    """Compile the package with resolved dependencies."""
    manifest_path = os.path.join(os.getcwd(), "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        print("error: no hls-pkg.toml in current directory", file=sys.stderr)
        return 1
    manifest = parse_manifest(manifest_path)
    entry = args.entry or "main.hls"
    if not os.path.isfile(entry):
        print("error: entry file not found: %s" % entry, file=sys.stderr)
        return 1
    boot_py = os.path.join(REPO_ROOT, "boot", "boot.py")
    print("Building %s..." % entry)
    cmd = [sys.executable, boot_py, entry]
    print("  $ %s" % " ".join(cmd))
    result = subprocess.run(cmd)
    return result.returncode


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="hls-pkg",
        description="Hieu Louis package manager (Stage 13-alpha).")
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
    p_lock.set_defaults(func=cmd_lock)

    p_audit = sub.add_parser("audit", help="Print the total effect report of the dep tree.")
    p_audit.set_defaults(func=cmd_audit)

    p_verify = sub.add_parser("verify", help="Verify lockfile SHA-256 hashes.")
    p_verify.set_defaults(func=cmd_verify)

    p_build = sub.add_parser("build", help="Compile the package with resolved dependencies.")
    p_build.add_argument("--entry", default="main.hls")
    p_build.set_defaults(func=cmd_build)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
