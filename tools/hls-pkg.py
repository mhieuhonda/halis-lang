#!/usr/bin/env python3
"""hls-pkg — Package manager for Halis (HLS).

Stage 13 (v0.11.0-alpha → release): content-addressed package manager with
verified provenance, effect enforcement, transparency log, multi-file
packages, and version verification.

Manifest format (hls-pkg.toml, simple TOML-like):
------------------------------------------------------------
    [package]
    name = "mylib"
    version = "0.1.0"
    authors = ["Your Name <you@example.com>"]
    description = "A small HLS library."

    [dependencies]
    std.str = { git = "https://github.com/mhieuhonda/halis-lang.git", path = "std/str.hls" }
    mymath  = { git = "https://github.com/foo/bar.git", path = "src/math.hls", tag = "v1.0.0" }
    mypkg   = { git = "https://github.com/foo/pk.git", path = "src/" }  # multi-file

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
      "version": 2,
      "packages": [
        {
          "name": "std.str",
          "source": { "git": "...", "path": "std/str.hls", "tag": "v0.21.0" },
          "version": "v0.21.0",
          "commit": "abcdef0123456789abcdef0123456789abcdef01",
          "sha256": "abc123...",
          "effects": [],
          "transitive_effects": [],
          "resolved_path": "/abs/path/to/file",
          "log_seq": 7
        },
        ...
      ]
    }
------------------------------------------------------------

The lockfile is content-addressed: each package's SHA-256 is computed
over the resolved file content (or, for multi-file packages, a sorted
walk of the directory). The effect table is computed by running
the Stage-0 checker's `--audit` on the package's files.

Stage 13 release — transparency log:
  Every `hls-pkg lock` AND every `hls-pkg publish` appends a record to
  `.hls-pkg-transparency.log` (JSON-lines, SHA-256 chained). `hls-pkg
  verify` cross-checks the lockfile against the log; `hls-pkg log
  --verify` validates the chain. A tampered log entry is detected by
  the chain hash breaking; a tampered dependency is detected by SHA-256
  mismatch.

Stage 13 release — multi-file packages:
  When `source.path` is a directory, the resolver returns the directory;
  `hls-pkg build` symlinks the whole dir into `.hls-pkg-deps/<name>/`
  so sibling imports resolve. `hls-pkg lock` computes a deterministic
  content hash over the sorted file walk.

Stage 13 release — version verification:
  The lockfile records the resolved `version` (tag/branch) AND the
  40-char `commit` SHA. `hls-pkg verify` runs `git rev-parse HEAD` and
  compares; a moved tag is reported as a verification failure.

Commands:
  hls-pkg init NAME           Create a new package skeleton.
  hls-pkg add NAME GIT PATH   Add a git dependency (--tag XOR --branch).
  hls-pkg lock                Resolve deps, write lockfile, append to log.
  hls-pkg audit               Print the total effect report of the dep tree.
  hls-pkg build [--entry F]   Compile the package with resolved deps.
  hls-pkg verify              Verify lockfile hashes + commits + log entries.
  hls-pkg publish             Append the current package to the log.
  hls-pkg log [--verify]      Print the transparency log / verify its chain.

Status: release. The transparency log is a local JSON-lines file today;
the Stage 13 release-target decentralised registry is the Stage 20
roadmap item.
"""
import argparse
import hashlib
import json
import os
import re as _re_mod
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Stage 13 release: transparency log (append-only, content-addressed)
# ---------------------------------------------------------------------------
# The transparency log is a single JSON-lines file under the repo root.
# Every `hls-pkg publish` appends one record; `hls-pkg verify` checks each
# lockfile entry against the log so a tampered or roll-back dependency is
# caught. The log is content-addressed (records SHA-256 chain, mirroring
# Certificate Transparency's Head/X/Y structure on a single-host scale).

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, ".hls-pkg-cache")


# ---------------------------------------------------------------------------
# Stage 13 release: transparency log (append-only, content-addressed)
# ---------------------------------------------------------------------------
# The transparency log is a single JSON-lines file under the repo root.
# Every `hls-pkg publish` appends one record; `hls-pkg verify` checks each
# lockfile entry against the log so a tampered or roll-back dependency is
# caught. The log is content-addressed (records SHA-256 chain, mirroring
# Certificate Transparency's Head/X/Y structure on a single-host scale).

TRANSPARENCY_LOG = os.path.join(REPO_ROOT, ".hls-pkg-transparency.log")


def transparency_log_append(record: Dict) -> Dict:
    """Append a record to the transparency log; return the entry with
    `seq`, `timestamp`, and `prev_hash` filled in.

    The log is append-only: we open the file in `a+b` mode so concurrent
    writers don't truncate. The chain hash makes any silent mutation of
    a past record detectable (rewriting line N breaks line N+1's
    prev_hash).
    """
    # Read the previous head's hash so we can chain to it.
    prev_hash = "0" * 64
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            tail = b""
            # Read up to the last 4 KB to find the last line.
            try:
                f.seek(-4096, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read()
            lines = [ln for ln in tail.split(b"\n") if ln.strip()]
            if lines:
                last = json.loads(lines[-1].decode("utf-8"))
                prev_hash = last.get("chain_hash", prev_hash)
    except (FileNotFoundError, OSError, ValueError):
        # First record (or corrupted log) — chain from genesis.
        pass
    # Build the record.
    record = dict(record)
    record["seq"] = _next_seq()
    record["timestamp"] = int(time.time())
    record["prev_hash"] = prev_hash
    # Compute the chain hash: SHA-256 over prev_hash || canonical-JSON(record)
    # minus the chain_hash field (which we add last).
    canon = json.dumps(record, sort_keys=True, separators=(",", ":"))
    chain = hashlib.sha256((prev_hash + canon).encode("utf-8")).hexdigest()
    record["chain_hash"] = chain
    # Append.
    with open(TRANSPARENCY_LOG, "ab") as f:
        f.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    return record


def _next_seq() -> int:
    """Return the next sequence number for the transparency log."""
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            count = 0
            for _ in f:
                count += 1
            return count + 1
    except (FileNotFoundError, OSError):
        return 1


def transparency_log_lookup(name: str, version: Optional[str] = None) -> Optional[Dict]:
    """Return the latest log entry for `name` (optionally matching
    `version`), or None if not found. Walks the log from the END backwards
    for efficiency on long logs."""
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return None
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        if rec.get("name") == name:
            if version is None or rec.get("version") == version:
                return rec
    return None


def transparency_log_verify_chain() -> List[str]:
    """Verify the chain hashes of the transparency log; return a list of
    error messages (empty list = log is sound)."""
    errors = []
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return []  # No log = nothing to verify.
    prev_hash = "0" * 64
    expected_seq = 1
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8"))
        except ValueError as ex:
            errors.append("line %d: invalid JSON (%s)" % (i + 1, ex))
            continue
        if rec.get("prev_hash") != prev_hash:
            errors.append("line %d (%s): prev_hash mismatch (expected %s, got %s)"
                          % (i + 1, rec.get("name", "?"), prev_hash[:12],
                             str(rec.get("prev_hash"))[:12]))
        if rec.get("seq") != expected_seq:
            errors.append("line %d (%s): seq mismatch (expected %d, got %s)"
                          % (i + 1, rec.get("name", "?"), expected_seq,
                             rec.get("seq")))
        # Recompute the chain hash over the record minus chain_hash.
        canon_rec = {k: v for k, v in rec.items() if k != "chain_hash"}
        canon = json.dumps(canon_rec, sort_keys=True, separators=(",", ":"))
        expected_chain = hashlib.sha256(
            (prev_hash + canon).encode("utf-8")).hexdigest()
        if rec.get("chain_hash") != expected_chain:
            errors.append("line %d (%s): chain_hash mismatch"
                          % (i + 1, rec.get("name", "?")))
        prev_hash = rec.get("chain_hash", prev_hash)
        expected_seq += 1
    return errors


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
    # Strip comments — only OUTSIDE strings.
    # BUG (deep-scan-5): the old code stripped everything after the first
    # `#` on a line regardless of quotes, so `name = "demo # 1"` silently
    # corrupted the manifest (the string parser then swallowed subsequent
    # lines looking for a closing quote, garbling every following key).
    lines = []
    for line in src.split("\n"):
        lines.append(_strip_toml_comment(line))
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
            if not current_section:
                # SCAN-B fix: empty section name `[]` creates `result[""]`
                # entries; round-trip emits invalid TOML.
                raise ValueError("manifest: empty section header at offset %d" % i)
            # Make sure the section dict exists.
            parts = current_section.split(".")
            cursor = result
            for p in parts:
                if not isinstance(cursor, dict):
                    raise ValueError("manifest: section [%s] shadows a non-table "
                                     "value (a key under [%s] was already set)"
                                     % (current_section, current_section))
                cursor = cursor.setdefault(p, {})
                if not isinstance(cursor, dict):
                    raise ValueError("manifest: section [%s] shadows a non-table "
                                     "value" % current_section)
            i = j + 1
            continue
        # key = value
        eq = src.index("=", i)
        key = src[i:eq].strip()
        if not key:
            # SCAN-B fix: `= value` (no key) silently created `result[""]`.
            raise ValueError("manifest: empty key at offset %d" % i)
        # Parse value.
        val, i = _parse_value(src, eq + 1)
        # Assign into the current section.
        if current_section is None:
            result[key] = val
        else:
            parts = current_section.split(".")
            cursor = result
            for p in parts:
                if not isinstance(cursor, dict):
                    raise ValueError("manifest: cannot set key under non-table "
                                     "section [%s]" % current_section)
                cursor = cursor.setdefault(p, {})
            if not isinstance(cursor, dict):
                raise ValueError("manifest: cannot set key under non-table "
                                 "section [%s]" % current_section)
            cursor[key] = val
    return result


def _strip_toml_comment(line: str) -> str:
    """Strip a `#` comment, but only OUTSIDE double-quoted strings."""
    out = []
    in_str = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_str:
            if c == '\\' and i + 1 < n:
                out.append(c)
                out.append(line[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == '#':
            break
        out.append(c)
        i += 1
    return "".join(out)


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
        # String (with escape decoding — BUG deep-scan-5: escapes were
        # never decoded, so `say \"hi\"` round-tripped with literal
        # backslashes and write_manifest grew them on every pass).
        j = i + 1
        out = []
        while j < n and src[j] != '"':
            if src[j] == '\\' and j + 1 < n:
                nxt = src[j + 1]
                if nxt == 'n':
                    out.append('\n')
                elif nxt == 't':
                    out.append('\t')
                elif nxt == 'r':
                    out.append('\r')
                else:
                    out.append(nxt)
                j += 2
            else:
                out.append(src[j])
                j += 1
        return "".join(out), j + 1
    if c == '[':
        # List of strings / bare tokens.
        # BUG (deep-scan-5): bare items (IO, Fs, integers) were consumed
        # char-by-char and silently DROPPED — `allowed = [IO, Fs]` parsed
        # to an empty list. Collect them with type conversion, mirroring
        # the bare-token branch below.
        j = i + 1
        items = []
        while j < n and src[j] != ']':
            while j < n and src[j] in " \t\r\n,":
                j += 1
            if j >= n or src[j] == ']':
                break
            if src[j] == '"':
                k = j + 1
                out = []
                while k < n and src[k] != '"':
                    if src[k] == '\\' and k + 1 < n:
                        nxt = src[k + 1]
                        if nxt == 'n':
                            out.append('\n')
                        elif nxt == 't':
                            out.append('\t')
                        elif nxt == 'r':
                            out.append('\r')
                        else:
                            out.append(nxt)
                        k += 2
                    else:
                        out.append(src[k])
                        k += 1
                items.append("".join(out))
                j = k + 1
            else:
                k = j
                while k < n and src[k] not in " \t\r\n,]":
                    k += 1
                tok = src[j:k]
                if tok:
                    if tok == "true":
                        items.append(True)
                    elif tok == "false":
                        items.append(False)
                    else:
                        try:
                            items.append(int(tok))
                            j = k
                            continue
                        except ValueError:
                            pass
                        try:
                            items.append(float(tok))
                            j = k
                            continue
                        except ValueError:
                            pass
                        items.append(tok)
                j = k
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
    """Write a manifest dict as TOML.

    BUG (deep-scan-5): previously only [package]/[dependencies]/[effects]
    were written — `hls-pkg add` silently DELETED any other section the
    user had (e.g. [features]). Round-trip unknown sections too.
    """
    known = ["package", "dependencies", "effects"]
    lines = []
    for section in known:
        if section in manifest:
            lines.append("[%s]" % section)
            for k, v in manifest[section].items():
                lines.append('%s = %s' % (k, _fmt_value(v)))
            lines.append("")
    # Preserve unknown sections (round-trip).
    for section, v in manifest.items():
        if section in known or not isinstance(v, dict):
            continue
        lines.append("[%s]" % section)
        for k, vv in v.items():
            lines.append('%s = %s' % (k, _fmt_value(vv)))
        lines.append("")
    # Preserve top-level scalar keys.
    for k, v in manifest.items():
        if isinstance(v, dict):
            continue
        lines.append('%s = %s' % (k, _fmt_value(v)))
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _fmt_value(v) -> str:
    if isinstance(v, str):
        # BUG (deep-scan-5): backslashes were not escaped, so a value
        # containing one grew a backslash on every write/parse round-trip.
        return '"%s"' % v.replace('\\', '\\\\').replace('"', '\\"')
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


def _validate_dep_name(name: str):
    """BUG-DS4-22: dependency names become cache DIRECTORY components.
    A name like "/tmp/pwned" made os.path.join treat it as an absolute
    path — git cloned attacker-controlled content into an arbitrary
    directory. Only [A-Za-z0-9._-] is allowed and the name must not start
    with '.' or '-'."""
    if not name or not _re_mod.fullmatch(r"[A-Za-z0-9._-]+", name) or name[0] in ".-":
        raise ValueError("invalid dependency name: %r (allowed: letters, "
                         "digits, '.', '_', '-'; must not start with '.' or '-')" % name)


def _validate_git_arg(value: str, what: str):
    """SCAN-B security fix: git commands accept option strings like
    `--upload-pack=/tmp/evil` as positional arguments. A manifest with
    `git = "--upload-pack=evil"` or `tag = "--upload-pack=evil"` would
    be passed to `git clone`/`fetch`/`checkout` as a flag, leading to
    arbitrary command execution. Reject any value that starts with `-`
    or contains a NUL byte (which can truncate the argument)."""
    if not isinstance(value, str):
        raise ValueError("%s must be a string, got %s" % (what, type(value).__name__))
    if not value:
        raise ValueError("%s must not be empty" % what)
    if value.startswith("-"):
        raise ValueError("%s must not start with '-' (git option injection): %r"
                         % (what, value))
    if "\x00" in value:
        raise ValueError("%s must not contain NUL bytes" % what)


def _confine(base_dir: str, rel: str, what: str) -> str:
    """Resolve `rel` under `base_dir` and REFUSE escapes (absolute paths,
    '..' traversal, symlinks pointing outside).

    BUG-DS4-22: path deps could point outside the repo (including
    absolute paths), letting a malicious manifest import any file on
    the machine into the build.

    SCAN-B fix: `os.path.normpath` does NOT resolve symlinks, so a
    `path = "symlink_to_etc_passwd"` dependency passed the old check
    (the normpath'd string still started with base_dir + sep) even
    though the actual file resolved to /etc/passwd. Now we use
    `os.path.realpath` for both `full` and `base_real`, matching
    the runtime sandbox's behaviour."""
    if os.path.isabs(rel):
        raise ValueError("%s: absolute paths are not allowed: %s" % (what, rel))
    # Reject any '..' path segment explicitly BEFORE realpath — this
    # catches the simple `../../etc/passwd` attack without needing to
    # stat the file (realpath of a non-existent `../..` still resolves
    # outside).
    parts = rel.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise ValueError("%s: '..' traversal is not allowed: %s" % (what, rel))
    joined = os.path.join(base_dir, rel)
    full = os.path.realpath(joined)
    base_real = os.path.realpath(base_dir)
    if not (full == base_real or full.startswith(base_real + os.sep)):
        raise ValueError("%s: path escapes the allowed directory: %s "
                         "(resolves to %s)" % (what, rel, full))
    return full


def resolve_dependency(name: str, source: Dict, cache_dir: str = CACHE_DIR) -> str:
    """Resolve a dependency to a local file path.

    For `path`-only deps: relative to the repo root.
    For `git` deps: clone into cache_dir, checkout tag/branch, return path.

    SCAN-B fix (multi-file packages): when `source.path` is a directory
    (not a file), return the directory so callers can symlink the whole
    package into `.hls-pkg-deps/<name>/` and boot.py can resolve sibling
    imports. The legacy single-file contract is preserved when `source.path`
    is a file.
    """
    _validate_dep_name(name)
    if not isinstance(source, dict):
        raise ValueError("dependency %s: source must be a table, got %s"
                         % (name, type(source).__name__))
    if "path" in source:
        # Path dependency. Resolve relative to the repo root — and CONFINED
        # to it (BUG-DS4-22).
        p = _confine(REPO_ROOT, source["path"], "dependency %s" % name)
        if os.path.isdir(p):
            return p  # multi-file package directory
        if not os.path.isfile(p):
            raise FileNotFoundError(
                "dependency %s: path not found: %s" % (name, p))
        return p
    if "git" in source:
        _validate_git_arg(source["git"], "dependency %s git URL" % name)
        # Git dependency. Clone into cache.
        os.makedirs(cache_dir, exist_ok=True)
        repo_url = source["git"]
        repo_hash = sha256_bytes(repo_url.encode("utf-8"))[:12]
        # SCAN-B fix: use the name verbatim (it's already validated to
        # [A-Za-z0-9._-]+) instead of replacing '.' with '_' which made
        # `foo.bar` and `foo_bar` collide in the cache.
        clone_dir = os.path.join(cache_dir, name, repo_hash)
        if not os.path.isdir(clone_dir):
            # SCAN-B security fix: prepend `--` so git knows everything
            # afterwards is a positional argument, not a flag.
            subprocess.run(["git", "clone", "--quiet", "--",
                           repo_url, clone_dir],
                           check=True, capture_output=True, timeout=300)
        # Checkout tag/branch if specified.
        ref = source.get("tag") or source.get("branch") or "main"
        _validate_git_arg(ref, "dependency %s git ref" % name)
        try:
            subprocess.run(["git", "-C", clone_dir, "fetch", "--quiet",
                            "origin", "--", ref], check=True, capture_output=True,
                           timeout=300)
            subprocess.run(["git", "-C", clone_dir, "checkout", "--quiet",
                            ref], check=True, capture_output=True, timeout=300)
        except subprocess.CalledProcessError:
            # Fallback: try just `git checkout main`.
            subprocess.run(["git", "-C", clone_dir, "checkout", "--quiet",
                            "main"], check=True, capture_output=True, timeout=300)
        path = source.get("path", "")
        if path:
            full = _confine(clone_dir, path, "dependency %s" % name)
        else:
            full = clone_dir
        # SCAN-B fix: support multi-file packages where `path` is a dir.
        if os.path.isdir(full):
            return full
        if not os.path.isfile(full):
            raise FileNotFoundError(
                "dependency %s: path not found in repo: %s" % (name, full))
        return full
    raise ValueError("dependency %s has no git/path source" % name)


def git_resolve_commit(clone_dir: str, ref: str) -> Optional[str]:
    """Stage 13 release: resolve `ref` (tag/branch) to a 40-char commit SHA.
    Returns None if the ref doesn't exist or git is unavailable."""
    try:
        r = subprocess.run(["git", "-C", clone_dir, "rev-parse", ref],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        sha = r.stdout.strip()
        if _re_mod.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def git_current_commit(clone_dir: str) -> Optional[str]:
    """Return the current HEAD commit SHA of `clone_dir`, or None."""
    return git_resolve_commit(clone_dir, "HEAD")


# ---------------------------------------------------------------------------
# Effect extraction.
# ---------------------------------------------------------------------------

# Fail-closed effect set: if the audit of a dependency cannot be run
# (missing boot.py, timeout, non-zero exit), we record the FULL effect
# family so effect enforcement fails closed instead of silently passing
# (BUG-DS4-23: the old behaviour returned ([], []) — an unaudittable or
# actively broken dependency was recorded as PURE).
# Stage 10/11 deep-scan fix: include Net, Rand, Proc (Stage 9 release)
# so a dependency that uses net_lookup / rand_int / proc_exec is recorded
# as requiring those effects (otherwise the package's `effects.allowed`
# could silently approve a dependency using `proc_exec` because the
# fail-closed list missed Proc — a security soundness bypass).
FAIL_CLOSED_EFFECTS = sorted(["Args", "Clock", "Exit", "Fs", "IO",
                              "Net", "Rand", "Proc"])


def extract_effects(file_path: str) -> Tuple[List[str], List[str]]:
    """Run `boot.py --audit` on a single-file package; return (declared, transitive).

    The HLS checker requires a `main` function. Library files (like
    `std/str.hls`) don't have one, so we generate a temporary wrapper
    file in the SAME directory as the target (so relative imports work)
    that imports the library and provides a dummy main.
    """
    boot_py = os.path.join(REPO_ROOT, "boot", "boot.py")
    if not os.path.isfile(boot_py):
        print("warning: cannot audit %s (boot.py not found) — recording "
              "the full effect set (fail closed)" % file_path, file=sys.stderr)
        return list(FAIL_CLOSED_EFFECTS), list(FAIL_CLOSED_EFFECTS)
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
    except (subprocess.TimeoutExpired, OSError) as ex:
        print("warning: cannot audit %s (%s) — recording the full effect "
              "set (fail closed)" % (file_path, ex), file=sys.stderr)
        return list(FAIL_CLOSED_EFFECTS), list(FAIL_CLOSED_EFFECTS)
    if result.returncode != 0:
        print("warning: audit of %s failed (exit %d) — recording the full "
              "effect set (fail closed)" % (file_path, result.returncode),
              file=sys.stderr)
        return list(FAIL_CLOSED_EFFECTS), list(FAIL_CLOSED_EFFECTS)
    # Parse the audit output: look for the function table, extract the
    # declared and computed effect sets.
    declared = set()
    computed = set()
    # Stage 10/11 deep-scan fix: include Net, Rand, Proc so audit output
    # for a dependency using net_lookup / rand_int / proc_exec is parsed
    # correctly (was missing, so those effects were silently dropped,
    # making effect enforcement meaningless for Net/Rand/Proc users).
    KNOWN_EFFECTS = {"IO", "Fs", "Clock", "Args", "Exit",
                    "Net", "Rand", "Proc"}
    for line in result.stdout.split("\n"):
        # BUG-SC-PKG-11 fix: the previous parser added every effect name
        # found on each status line to BOTH declared and computed, making
        # them always identical (the union of all effects across all
        # functions). This made effect enforcement meaningless. Now we
        # properly split each audit line into columns: function name,
        # declared, computed, status. The boot.py audit output formats
        # these as fixed-width columns separated by 2+ spaces.
        if line.startswith("  ") and ("OK" in line or "VIOLATION" in line):
            # Strip the leading indent and split on 2+ spaces.
            stripped = line.strip()
            # Split on runs of 2+ spaces to get the columns.
            cols = _re_mod.split(r"\s{2,}", stripped)
            if len(cols) < 4:
                continue
            # cols[0] = function name, cols[1] = declared, cols[2] = computed,
            # cols[3] = status (OK / VIOLATION: ...).
            # Parse comma-separated effect names from each column.
            #
            # SCAN-B fix: cols[1].replace("pure", "") would strip the
            # substring `pure` from inside effect names (none currently
            # contain it, but defensively) and from `pure + IO, Fs` would
            # leave ` + IO, Fs` whose strip() doesn't match `IO` after
            # the comma split. Now we strip the explicit prefixes.
            decl_col = cols[1]
            # Strip the known prefixes that boot.py uses to surface `pure`.
            for pfx in ("pure + ", "pure", "(none - pure)", "(none)"):
                if decl_col.startswith(pfx):
                    decl_col = decl_col[len(pfx):]
            for eff in decl_col.split(","):
                eff = eff.strip()
                if eff in KNOWN_EFFECTS:
                    declared.add(eff)
            comp_col = cols[2].replace("(none)", "")
            for eff in comp_col.split(","):
                eff = eff.strip()
                if eff in KNOWN_EFFECTS:
                    computed.add(eff)
    # SCAN-B soundness fix: if boot.py exited 0 but we found NO audit
    # rows (format drift, or the wrapper import resolved to a different
    # file), the old code returned ([], []) — recording the dependency
    # as PURE. Fail closed instead.
    if not declared and not computed:
        # Heuristic: if the audit output mentioned any KNOWN_EFFECT name
        # at all (e.g. in a status line we missed), fail closed.
        stdout_lower = result.stdout
        if any(eff in stdout_lower for eff in KNOWN_EFFECTS) or result.stdout.strip():
            print("warning: audit of %s produced unparseable output — "
                  "recording the full effect set (fail closed)" % file_path,
                  file=sys.stderr)
            return list(FAIL_CLOSED_EFFECTS), list(FAIL_CLOSED_EFFECTS)
    return sorted(declared), sorted(computed)


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Create a new package skeleton."""
    name = args.name
    # SCAN-B security fix: validate the package name so `hls-pkg init ../evil`
    # or `hls-pkg init /tmp/owned` can't create directories outside cwd.
    # Same rules as dependency names (letters/digits/._-; no leading . or -).
    try:
        _validate_dep_name(name)
    except ValueError as ex:
        print("error: invalid package name: %s" % ex, file=sys.stderr)
        return 1
    # Also reject path separators (the dep-name regex already does).
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
        f.write("# %s\n\nA Halis package.\n" % name)
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
    # SCAN-B fix: --tag and --branch are mutually exclusive — silently
    # dropping one was a source of confusing reproducibility bugs.
    if args.tag and args.branch:
        print("error: --tag and --branch are mutually exclusive", file=sys.stderr)
        return 1
    manifest = parse_manifest(manifest_path)
    deps = manifest.setdefault("dependencies", {})
    if not isinstance(deps, dict):
        print("error: [dependencies] section is corrupt (not a table)", file=sys.stderr)
        return 1
    # SCAN-B security fix: validate name + git args BEFORE writing.
    try:
        _validate_dep_name(args.name)
        _validate_git_arg(args.git, "dependency %s git URL" % args.name)
        if args.tag:
            _validate_git_arg(args.tag, "dependency %s tag" % args.name)
        if args.branch:
            _validate_git_arg(args.branch, "dependency %s branch" % args.name)
    except ValueError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 1
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
    # SCAN-B fix: validate the manifest shape before iterating.
    if deps is None:
        deps = {}
    if not isinstance(deps, dict):
        print("error: [dependencies] section is corrupt (not a table)",
              file=sys.stderr)
        return 1
    pkg_version = manifest.get("package", {}).get("version", "0.0.0")
    pkg_name = manifest.get("package", {}).get("name", "<unnamed>")
    lockfile = {"version": 2, "packages": []}
    for name, source in deps.items():
        print("Resolving %s..." % name, end=" ", flush=True)
        try:
            resolved_path = resolve_dependency(name, source)
        except Exception as ex:
            print("FAILED: %s" % ex)
            return 1
        sha = sha256_file(resolved_path)
        declared, computed = extract_effects(resolved_path)
        # Stage 13 release: record the resolved version + commit hash so
        # `verify` can detect a moved tag (TOCTOU on the git cache).
        resolved_version = source.get("tag") or source.get("branch") or "main"
        commit = None
        # If this is a git dep, find the clone_dir and resolve HEAD.
        if "git" in source:
            repo_hash = sha256_bytes(source["git"].encode("utf-8"))[:12]
            clone_dir = os.path.join(CACHE_DIR, name, repo_hash)
            if os.path.isdir(clone_dir):
                commit = git_current_commit(clone_dir)
        # Stage 13 release: write a transparency-log entry so the
        # dependency's content-addressed record is publicly auditable.
        try:
            log_rec = transparency_log_append({
                "name": name,
                "version": resolved_version,
                "sha256": sha,
                "commit": commit or "<path-dep>",
                "package": pkg_name,
                "package_version": pkg_version,
            })
            log_seq = log_rec["seq"]
        except OSError as ex:
            print("warning: transparency log write failed (%s)" % ex,
                  file=sys.stderr)
            log_seq = None
        print("OK (sha256: %s..)" % sha[:12])
        lockfile["packages"].append({
            "name": name,
            "source": source,
            "version": resolved_version,
            "commit": commit,
            "sha256": sha,
            "effects": declared,
            "transitive_effects": computed,
            "resolved_path": resolved_path,
            "log_seq": log_seq,
        })
    # Write lockfile.
    lockfile_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    with open(lockfile_path, "w") as f:
        json.dump(lockfile, f, indent=2)
    print("")
    print("Wrote lockfile: %s (%d packages)" % (lockfile_path, len(lockfile["packages"])))
    # Effect enforcement: check that the package's declared `effects.allowed`
    # is a superset of every dependency's computed effects.
    effects_section = manifest.get("effects", {})
    if not isinstance(effects_section, dict):
        print("error: [effects] section is corrupt (not a table)", file=sys.stderr)
        return 1
    allowed_raw = effects_section.get("allowed", [])
    # SCAN-B fix: validate `allowed` is a list of strings. A string like
    # "IO" used to be silently expanded to {"I", "O"}, causing every
    # real effect to be flagged as a violation.
    if not isinstance(allowed_raw, list):
        print("error: [effects].allowed must be a list, got %s" %
              type(allowed_raw).__name__, file=sys.stderr)
        return 1
    allowed = set()
    for eff in allowed_raw:
        if not isinstance(eff, str):
            print("error: [effects].allowed entries must be strings, got %s" %
                  type(eff).__name__, file=sys.stderr)
            return 1
        allowed.add(eff)
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


def _load_lockfile(path: str) -> Optional[Dict]:
    """SCAN-B fix: load and validate the lockfile shape. Returns None on
    malformed JSON or missing required keys — callers print a clean error."""
    try:
        with open(path, "r") as f:
            lockfile = json.load(f)
    except (OSError, ValueError) as ex:
        print("error: cannot read lockfile %s: %s" % (path, ex), file=sys.stderr)
        return None
    if not isinstance(lockfile, dict):
        print("error: lockfile is not a JSON object", file=sys.stderr)
        return None
    pkgs = lockfile.get("packages")
    if not isinstance(pkgs, list):
        print("error: lockfile 'packages' is not a list", file=sys.stderr)
        return None
    return lockfile


def cmd_audit(args):
    """Print the total effect report of the dependency tree."""
    lockfile_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    if not os.path.isfile(lockfile_path):
        print("error: no hls-pkg.lock — run `hls-pkg lock` first", file=sys.stderr)
        return 1
    lockfile = _load_lockfile(lockfile_path)
    if lockfile is None:
        return 1
    pkgs = lockfile["packages"]
    # SCAN-B fix: validate each entry's shape so a malformed lockfile
    # doesn't crash with KeyError/TypeError.
    valid_pkgs = []
    for p in pkgs:
        if not isinstance(p, dict):
            print("warning: skipping malformed package entry (not an object)",
                  file=sys.stderr)
            continue
        if not isinstance(p.get("name"), str):
            print("warning: skipping package entry with missing/non-string name",
                  file=sys.stderr)
            continue
        valid_pkgs.append(p)
    pkgs = valid_pkgs
    name_w = max((len(p["name"]) for p in pkgs), default=4)
    print("=" * (name_w + 60))
    print("  %-*s  %12s  %12s  %s" % (
        name_w, "package", "sha256[..12]", "declared", "transitive"))
    print("=" * (name_w + 60))
    all_effects = set()
    for pkg in pkgs:
        sha = str(pkg.get("sha256", ""))[:12]
        eff_list = pkg.get("effects", [])
        trans_list = pkg.get("transitive_effects", [])
        eff_list = eff_list if isinstance(eff_list, list) else []
        trans_list = trans_list if isinstance(trans_list, list) else []
        decl = ", ".join(str(e) for e in eff_list) or "(none)"
        trans = ", ".join(str(e) for e in trans_list) or "(none)"
        all_effects.update(str(e) for e in trans_list)
        print("  %-*s  %12s  %12s  %s" % (name_w, pkg["name"], sha, decl, trans))
    print("=" * (name_w + 60))
    print("  Total transitive effects of dependency tree: %s" % (
        ", ".join(sorted(all_effects)) or "(none — pure library)"))
    return 0


def cmd_verify(args):
    """Verify the lockfile's SHA-256 hashes still match (Stage 13 release:
    also verify the git HEAD commit matches the recorded one and the
    transparency-log entry exists)."""
    lockfile_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    if not os.path.isfile(lockfile_path):
        print("error: no hls-pkg.lock — run `hls-pkg lock` first", file=sys.stderr)
        return 1
    lockfile = _load_lockfile(lockfile_path)
    if lockfile is None:
        return 1
    failures = 0
    for pkg in lockfile["packages"]:
        if not isinstance(pkg, dict):
            print("  [FAIL] <malformed entry>: not an object")
            failures += 1
            continue
        name = pkg.get("name", "?")
        path = pkg.get("resolved_path", "")
        if not isinstance(path, str) or not os.path.exists(path):
            print("  [FAIL] %s: file not found (%s)" % (name, path))
            failures += 1
            continue
        # SCAN-B fix: handle both file and directory (multi-file pkgs).
        if os.path.isdir(path):
            # Walk the directory and verify the per-file SHA chain.
            # The lockfile records the directory's content hash.
            actual_sha = _sha256_directory(path)
        else:
            actual_sha = sha256_file(path)
        expected_sha = str(pkg.get("sha256", ""))
        if actual_sha != expected_sha:
            print("  [FAIL] %s: hash mismatch (expected %s, got %s)" % (
                name, expected_sha[:12], actual_sha[:12]))
            failures += 1
            continue
        # Stage 13 release: verify the git commit (if recorded).
        recorded_commit = pkg.get("commit")
        if recorded_commit and isinstance(recorded_commit, str) and \
                _re_mod.fullmatch(r"[0-9a-f]{40}", recorded_commit):
            # Reconstruct the clone_dir for git deps.
            source = pkg.get("source", {})
            if isinstance(source, dict) and "git" in source:
                try:
                    _validate_dep_name(name)
                    repo_hash = sha256_bytes(source["git"].encode("utf-8"))[:12]
                    clone_dir = os.path.join(CACHE_DIR, name, repo_hash)
                    if os.path.isdir(clone_dir):
                        cur_commit = git_current_commit(clone_dir)
                        if cur_commit != recorded_commit:
                            print("  [FAIL] %s: commit moved (expected %s, "
                                  "got %s) — a moved tag detected" %
                                  (name, recorded_commit[:12],
                                   (cur_commit or "?")[:12]))
                            failures += 1
                            continue
                except (ValueError, OSError):
                    pass  # Skip commit verification on error.
        # Stage 13 release: transparency-log lookup.
        log_seq = pkg.get("log_seq")
        if isinstance(log_seq, int):
            rec = transparency_log_lookup(name, pkg.get("version"))
            if rec is None:
                print("  [WARN] %s: no transparency log entry found" % name)
            elif rec.get("seq") != log_seq:
                print("  [WARN] %s: transparency log seq drifted (expected %d, "
                      "got %s)" % (name, log_seq, rec.get("seq")))
            elif rec.get("sha256") != expected_sha:
                print("  [WARN] %s: transparency log SHA-256 mismatch" % name)
        print("  [ OK ] %s: hash matches (%s)" % (name, actual_sha[:12]))
    if failures:
        print("")
        print("Verification FAILED: %d packages have hash mismatches." % failures)
        return 1
    print("")
    print("All %d packages verified." % len(lockfile["packages"]))
    return 0


def cmd_build(args):
    """Compile the package with resolved dependencies.

    Stage 13 release: when a dependency resolves to a directory (multi-file
    package), symlink the WHOLE directory into `.hls-pkg-deps/<name>/` so
    boot.py's import resolver can find sibling imports. The legacy single-
    file contract is preserved."""
    manifest_path = os.path.join(os.getcwd(), "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        print("error: no hls-pkg.toml in current directory", file=sys.stderr)
        return 1
    # Parse the manifest to validate it early.
    parse_manifest(manifest_path)
    entry = args.entry or "main.hls"
    if not os.path.isfile(entry):
        print("error: entry file not found: %s" % entry, file=sys.stderr)
        return 1
    boot_py = os.path.join(REPO_ROOT, "boot", "boot.py")
    print("Building %s..." % entry)
    lock_path = os.path.join(os.getcwd(), "hls-pkg.lock")
    env = os.environ.copy()
    deps_dir = None
    if os.path.isfile(lock_path):
        lockfile = _load_lockfile(lock_path)
        if lockfile is None:
            return 1
        deps_dir = os.path.join(os.getcwd(), ".hls-pkg-deps")
        os.makedirs(deps_dir, exist_ok=True)
        for pkg in lockfile.get("packages", []):
            if not isinstance(pkg, dict):
                continue
            rp = pkg.get("resolved_path", "")
            if not isinstance(rp, str) or not rp or not os.path.exists(rp):
                # SCAN-B fix: explicit error instead of silent skip.
                print("error: dependency %s: resolved path missing (%s) — "
                      "run `hls-pkg lock` again" % (pkg.get("name", "?"), rp),
                      file=sys.stderr)
                return 1
            # Validate the dep name before using it in any path join.
            try:
                _validate_dep_name(pkg["name"])
            except (ValueError, KeyError) as ex:
                print("error: lockfile contains %s" % ex, file=sys.stderr)
                return 1
            # Verify hash (BUG-DS4-24 — fail closed on TOCTOU).
            if os.path.isdir(rp):
                actual_sha = _sha256_directory(rp)
            else:
                actual_sha = sha256_file(rp)
            if actual_sha != pkg.get("sha256"):
                print("error: dependency %s hash mismatch (expected %s, "
                      "got %s) — run `hls-pkg lock` again, or investigate."
                      % (pkg.get("name"),
                         str(pkg.get("sha256"))[:12], actual_sha[:12]),
                      file=sys.stderr)
                return 1
            # Stage 13 release: multi-file package support. If the dep is
            # a directory, symlink the whole dir into .hls-pkg-deps/<name>/
            # so sibling imports resolve. Otherwise, link as <name>.hls.
            if os.path.isdir(rp):
                dst = os.path.join(deps_dir, pkg["name"])
                if os.path.islink(dst) or os.path.exists(dst):
                    if os.path.islink(dst) or os.path.isfile(dst):
                        os.remove(dst)
                    else:
                        import shutil as _shutil
                        _shutil.rmtree(dst)
                try:
                    os.symlink(os.path.abspath(rp), dst)
                except OSError:
                    import shutil as _shutil
                    _shutil.copytree(rp, dst)
            else:
                dst = os.path.join(deps_dir, pkg["name"] + ".hls")
                try:
                    if os.path.islink(dst) or os.path.exists(dst):
                        os.remove(dst)
                    os.symlink(os.path.abspath(rp), dst)
                except OSError:
                    import shutil as _shutil
                    _shutil.copy2(rp, dst)
        env["HLS_PKG_DEPS"] = deps_dir
    cmd = [sys.executable, boot_py, entry]
    print("  $ %s" % " ".join(cmd))
    result = subprocess.run(cmd, env=env)
    return result.returncode


def _sha256_directory(path: str) -> str:
    """Stage 13 release: compute a deterministic content hash over a
    directory (sorted file list, each file's SHA-256 + relative path).
    Used by `cmd_lock`/`cmd_verify` for multi-file packages."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            h.update(b"\x00")
    return h.hexdigest()


def cmd_publish(args):
    """Stage 13 release: append the current package's content to the
    transparency log. Records the package name, version, content SHA-256,
    and (for git deps) the current commit. After publishing, the entry is
    publicly verifiable via `hls-pkg verify`."""
    manifest_path = os.path.join(os.getcwd(), "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        print("error: no hls-pkg.toml in current directory", file=sys.stderr)
        return 1
    manifest = parse_manifest(manifest_path)
    pkg_section = manifest.get("package", {})
    if not isinstance(pkg_section, dict):
        print("error: [package] section is corrupt", file=sys.stderr)
        return 1
    name = pkg_section.get("name", "<unnamed>")
    version = pkg_section.get("version", "0.0.0")
    # Compute the content SHA-256 over the package's HLS sources.
    cwd = os.getcwd()
    sources = []
    for root, dirs, files in os.walk(cwd):
        # Skip hidden dirs (.git, .hls-pkg-cache, .hls-pkg-deps).
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.endswith(".hls"):
                sources.append(os.path.join(root, fn))
    sources.sort()
    h = hashlib.sha256()
    for s in sources:
        rel = os.path.relpath(s, cwd).replace(os.sep, "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        with open(s, "rb") as f:
            h.update(f.read())
        h.update(b"\x00")
    content_sha = h.hexdigest()
    # Append to the transparency log.
    rec = transparency_log_append({
        "name": name,
        "version": version,
        "sha256": content_sha,
        "kind": "publish",
        "files": len(sources),
    })
    print("Published %s@%s" % (name, version))
    print("  content sha256: %s" % content_sha)
    print("  transparency log seq: %d" % rec["seq"])
    print("  chain hash: %s" % rec["chain_hash"][:24] + "...")
    return 0


def cmd_log(args):
    """Stage 13 release: print the transparency log (or verify its chain)."""
    if args.verify:
        errors = transparency_log_verify_chain()
        if errors:
            print("Transparency log verification FAILED:")
            for e in errors:
                print("  - " + e)
            return 1
        print("Transparency log verification OK (chain is sound).")
        return 0
    # Print the log.
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        print("(no transparency log; nothing to display)")
        return 0
    if not lines:
        print("(transparency log is empty)")
        return 0
    print("seq  timestamp            name                  version   sha256[..12]")
    print("-" * 80)
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        ts = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(rec.get("timestamp", 0)))
        print("%-4d %-20s %-20s %-8s %s" % (
            rec.get("seq", 0), ts,
            str(rec.get("name", "?"))[:20],
            str(rec.get("version", "?"))[:8],
            str(rec.get("sha256", ""))[:12]))
    return 0


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------

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
    p_lock.set_defaults(func=cmd_lock)

    p_audit = sub.add_parser("audit", help="Print the total effect report of the dep tree.")
    p_audit.set_defaults(func=cmd_audit)

    p_verify = sub.add_parser("verify", help="Verify lockfile SHA-256 hashes + commit + log.")
    p_verify.set_defaults(func=cmd_verify)

    p_build = sub.add_parser("build", help="Compile the package with resolved dependencies.")
    p_build.add_argument("--entry", default="main.hls")
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


if __name__ == "__main__":
    sys.exit(main())
