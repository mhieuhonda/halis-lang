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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(REPO_ROOT, ".hls-pkg-cache")
TRANSPARENCY_LOG = os.path.join(REPO_ROOT, ".hls-pkg-transparency.log")




__all__ = [
    "CACHE_DIR",
    "Dict",
    "List",
    "Optional",
    "REPO_ROOT",
    "TRANSPARENCY_LOG",
    "Tuple",
    "_re_mod",
    "argparse",
    "hashlib",
    "json",
    "os",
    "subprocess",
    "sys",
    "time",
]
