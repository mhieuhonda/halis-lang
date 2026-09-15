"""util - verbatim segment of the original tools/hls-pkg.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hpkg_common import (
    _re_mod, hashlib, os,
)

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




__all__ = [
    "_confine",
    "_validate_dep_name",
    "_validate_git_arg",
    "sha256_bytes",
    "sha256_file",
]
