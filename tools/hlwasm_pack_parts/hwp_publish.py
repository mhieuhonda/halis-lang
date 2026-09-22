"""hwp_publish — registry publish helper for Stage 76.

``publish`` is DRY-RUN BY DEFAULT: it validates the tarball +
registry URL + access level and prints the exact ``npm publish``
command WITHOUT touching the network. A real publish requires the
explicit ``--no-dry-run`` flag AND an installed ``npm`` binary.

This keeps the acceptance test hermetic (no credentials, no network)
while giving users the one command they need for release day.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, Optional

from hwp_common import log_info, log_warn

# Deep-scan-30 fix: the old pattern was `[^\\s]` inside a raw string —
# a character class of "anything but a backslash and the letter s", NOT
# "anything but whitespace". Registry paths containing an 's' were
# rejected (and URLs with a space accepted). `/\S*` is the intended
# "optional /path" part.
_REGISTRY_RE = re.compile(r"^https?://[^/\s]+(/\S*)?$")


def validate_registry(registry: str) -> str:
    if not _REGISTRY_RE.match(registry or ""):
        raise ValueError(
            "invalid registry URL %r (expected https://...)" % (registry,))
    return registry.rstrip("/")


def publish_command(tgz_path: str, registry: Optional[str] = None,
                    access: str = "public", tag: str = "latest") -> list:
    """Build the ``npm publish`` argv for ``tgz_path``."""
    if access not in ("public", "restricted"):
        raise ValueError("access must be public|restricted, got %r" % access)
    if not tag or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$", tag):
        raise ValueError("invalid dist-tag %r" % (tag,))
    cmd = ["npm", "publish", tgz_path, "--tag", tag, "--access", access]
    if registry:
        cmd += ["--registry", validate_registry(registry)]
    return cmd


def publish(tgz_path: str, registry: Optional[str] = None,
            access: str = "public", tag: str = "latest",
            dry_run: bool = True) -> Dict[str, Any]:
    """Publish ``tgz_path``; return a result dict (never raises on npm)."""
    if not os.path.isfile(tgz_path):
        raise ValueError("tarball not found: %r" % tgz_path)
    cmd = publish_command(tgz_path, registry, access, tag)
    result: Dict[str, Any] = {
        "tarball": os.path.abspath(tgz_path),
        "registry": validate_registry(registry) if registry else
        "https://registry.npmjs.org",
        "access": access,
        "tag": tag,
        "dry_run": dry_run,
        "command": " ".join(cmd),
        "published": False,
    }
    if dry_run:
        log_info("dry-run: %s" % result["command"])
        return result
    npm = shutil.which("npm")
    if npm is None:
        log_warn("npm not found; cannot publish (dry-run the command: %s)"
                 % result["command"])
        result["error"] = "npm not installed"
        return result
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120)
    except (OSError, subprocess.SubprocessError) as ex:
        result["error"] = str(ex)
        return result
    result["returncode"] = proc.returncode
    result["stdout"] = proc.stdout[-2000:]
    result["stderr"] = proc.stderr[-2000:]
    result["published"] = proc.returncode == 0
    if result["published"]:
        log_info("published %s [%s]" % (tgz_path, tag))
    else:
        log_warn("npm publish failed (rc=%d)" % proc.returncode)
    return result


__all__ = [
    "publish",
    "publish_command",
    "validate_registry",
]
