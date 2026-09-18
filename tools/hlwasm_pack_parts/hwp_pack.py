"""hwp_pack — tarball packer for Stage 76.

``pack`` turns a built ``pkg/`` dir into a publishable
``<stem>-<version>.tgz`` (stdlib ``tarfile`` + gzip — no npm needed)
plus a ``.pack-manifest.json``-verified file list. ``unpack`` reverses
it with the realpath containment defence (a malicious tarball with
``../../evil`` members or absolute paths is rejected, never written).

The tarball layout mirrors ``npm pack``: files live under
``package/<fname>`` so ``npm publish <tgz>`` accepts the artifact.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from typing import Any, Dict, List, Optional

from hwp_common import ensure_inside, log_info


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack(pkg_dir: str, tgz_path: Optional[str] = None) -> str:
    """Pack ``pkg_dir`` into a ``.tgz``; return the tarball path."""
    manifest_path = os.path.join(pkg_dir, ".pack-manifest.json")
    if not os.path.isfile(manifest_path):
        raise ValueError(
            "not a built package: %r is missing .pack-manifest.json "
            "(run 'hls-wasm-pack build' first)" % pkg_dir)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest: Dict[str, Any] = json.load(f)
    files: List[str] = sorted(manifest.get("files", {}).keys())
    if not files:
        raise ValueError("manifest lists no files; refusing to pack")
    for rel in ("package.json",):
        if rel not in files:
            raise ValueError("manifest is missing required %r" % rel)

    name = str(manifest.get("name", "halis-app"))
    version = str(manifest.get("version", "0.1.0"))
    stem = name.rsplit("/", 1)[-1].lstrip("@") or "halis-pkg"
    if tgz_path is None:
        tgz_path = os.path.join(os.path.dirname(os.path.abspath(pkg_dir))
                                or ".",
                                "%s-%s.tgz" % (stem, version))
    out_abs = os.path.abspath(tgz_path)
    parent = os.path.dirname(out_abs) or "."
    os.makedirs(parent, exist_ok=True)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for fname in files:
            src = os.path.join(pkg_dir, fname)
            ensure_inside(src, pkg_dir, "package file")
            if not os.path.isfile(src):
                raise ValueError("listed file missing: %r" % fname)
            with open(src, "rb") as f:
                data = f.read()
            expect = manifest["files"][fname].get("sha256")
            if expect is not None and _sha256_bytes(data) != expect:
                raise ValueError(
                    "hash mismatch for %r (pkg/ changed after build; "
                    "rebuild before packing)" % fname)
            ti = tarfile.TarInfo(name="package/" + fname)
            ti.size = len(data)
            ti.mtime = 0  # reproducible tarballs (same bytes every run)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    with open(out_abs, "wb") as f:
        f.write(gzip.compress(raw, compresslevel=9, mtime=0))
    log_info("packed %d files -> %s (%d bytes)"
             % (len(files), out_abs, os.path.getsize(out_abs)))
    return out_abs


def list_members(tgz_path: str) -> List[str]:
    """Return the member names of ``tgz_path`` (no extraction)."""
    with tarfile.open(tgz_path, mode="r:gz") as tf:
        return tf.getnames()


def unpack(tgz_path: str, dest_dir: str) -> List[str]:
    """Unpack ``tgz_path`` into ``dest_dir``; return extracted paths.

    Every member is realpath-checked: absolute paths, ``..`` segments,
    symlinks and hardlinks are rejected (mirrors the Stage 27/75
    traversal defence on the serve side).
    """
    os.makedirs(dest_dir, exist_ok=True)
    extracted: List[str] = []
    with tarfile.open(tgz_path, mode="r:gz") as tf:
        for member in tf.getmembers():
            arcname = member.name
            if os.path.isabs(arcname) or ".." in arcname.split("/"):
                raise ValueError(
                    "tarball member escapes the package: %r (rejected)"
                    % arcname)
            if member.issym() or member.islnk():
                raise ValueError(
                    "tarball member is a link (rejected): %r" % arcname)
            target = os.path.join(dest_dir, arcname)
            ensure_inside(target, dest_dir, "tarball member")
            tf.extract(member, dest_dir)
            extracted.append(target)
    return extracted


__all__ = [
    "list_members",
    "pack",
    "unpack",
]
