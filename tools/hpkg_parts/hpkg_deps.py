"""deps - verbatim segment of the original tools/hls-pkg.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hpkg_common import (
    CACHE_DIR, Dict, List, Optional, REPO_ROOT, Tuple, _re_mod, os,
    subprocess, sys,
)
from hpkg_util import (
    _confine, _validate_dep_name, _validate_git_arg, sha256_bytes,
)

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
        # Deep-scan-15 fix (MEDIUM severity, supply-chain): the previous
        # implementation silently fell back to `git checkout main` if the
        # fetch/checkout of the declared `ref` failed. A moved tag, a
        # force-pushed branch, or a typo would then fetch the WRONG code
        # (the maintainer's `main` HEAD, which may have been compromised
        # since the lockfile was written). Surface the failure loudly so
        # the user knows their pinned dependency is unreachable instead
        # of silently substituting it.
        try:
            subprocess.run(["git", "-C", clone_dir, "fetch", "--quiet",
                            "origin", "--", ref], check=True, capture_output=True,
                           timeout=300)
            subprocess.run(["git", "-C", clone_dir, "checkout", "--quiet",
                            ref], check=True, capture_output=True, timeout=300)
        except subprocess.CalledProcessError as ex:
            raise RuntimeError(
                "dependency %s: failed to fetch/checkout git ref '%s' "
                "(declared in hls-pkg.lock). The previous behaviour silently "
                "fell back to 'main' — a supply-chain risk if the ref was "
                "force-pushed or the tag was moved. Inspect the upstream "
                "repository or update the lockfile. Git stderr: %s"
                % (name, ref, ex.stderr.decode("utf-8", "replace") if ex.stderr else "")
            ) from ex
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
    # Deep-scan-20 fix (LOW, race): the previous fixed filename
    # `.hls-pkg-audit-wrapper.hls` collided when two concurrent
    # `hls-pkg lock` runs audited the same dependency in the same
    # target_dir — one would overwrite the other's wrapper, then
    # the other's `finally: os.unlink` would delete the wrong file.
    # Include the PID for uniqueness.
    wrapper_path = os.path.join(
        target_dir, ".hls-pkg-audit-wrapper-%d.hls" % os.getpid())
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



__all__ = [
    "FAIL_CLOSED_EFFECTS",
    "extract_effects",
    "git_current_commit",
    "git_resolve_commit",
    "resolve_dependency",
]
