#!/usr/bin/env python3
"""hls-audit — the Stage 103 supply-chain effect report (transitive).

Usage:
  python3 tools/hls-audit.py <entry.hls> [--allow E1,E2,...] [--json]
  python3 tools/hls-audit.py --pkg [DIR] [--allow E1,E2,...] [--json]

Two modes, one report shape:

  source   walk the transitive `import` graph of an entry file (the
           SAME resolution rules boot.py uses — std./core. prefixes,
           relative paths, the HLS_PKG_DEPS fallback). Every module in
           the tree is classified (toolchain / dependency /
           workspace), every function is attributed to the module that
           defines it, and the report splits each module's effect
           surface into INTRINSIC (what its own bodies perform:
           builtin calls plus the declared effects of the externs they
           call) and REACHABLE (the checker fixpoint's set — its
           surface including everything it imports). For every effect
           in the program surface the report names EVERY introduction
           point and the chain of imports by which it enters the root.

  package  walk the transitive PACKAGE tree: the root manifest's
           [dependencies], then each dependency's OWN manifest, then
           its dependencies — the walk `hls-pkg audit` never does (it
           prints the top level flat). Each package's entry is audited
           through the real checker; a library entry (no main) is
           audited through the same wrapper contract `hls-pkg lock`
           uses (a generated wrapper imports the entry and declares a
           pure main). Nested `path` deps resolve package-relative
           first, repo-root-relative second (both confined — no ..
           escapes). A cycle is refused with the chain that closes it.
           When a lockfile exists, every top-level dependency's
           sha256 is recomputed against it: a mismatch is DRIFT and
           fails the run, because an audit of drifted content
           describes a tree the lock no longer names.

The policy gate: --allow IO,Fs (or the root manifest's
[effects].allowed in package mode) must cover every effect the
SUPPLY CHAIN introduces. The root is your code — reported, never
gated (the same division `hls-pkg lock` draws). Violations print the
introducing module/package and the full chain from the root.

Fail-closed, inherited from hls-pkg: a package that cannot be audited
(parse error, check error, unreadable) is NEVER recorded as pure — it
is reported UNAUDITABLE, its surface becomes the full effect set, and
the run fails. A source tree that cannot compile is not a supply-chain
finding at all: source mode says so and exits 1.

Exit contract: 0 when the report is produced and nothing violates the
policy; 1 on a policy violation, drift, a cycle, an unauditable
package, or an unauditable source tree; 2 for usage errors.

--json emits the same report as a machine-readable object
(schema "hls-audit/v1") for CI consumption. Analysis-only: nothing is
compiled, no artifact changes, neither front-end is affected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOL_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
_PKG = os.path.join(TOOL_DIR, "hpkg_parts")
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from hpkg_manifest import parse_manifest                    # noqa: E402
from hpkg_deps import resolve_dependency                    # noqa: E402
from hpkg_util import sha256_file                           # noqa: E402

from boot.boot import load_program, _resolve_import         # noqa: E402
from boot.checker import check                              # noqa: E402
from boot.lexer import tokenize                              # noqa: E402
from boot.parser import Parser                              # noqa: E402
from boot.checking.helpers import BUILTIN_EFFECTS           # noqa: E402

ALL_EFFECTS = sorted(["IO", "Fs", "Clock", "Args", "Exit",
                      "Net", "Rand", "Proc", "Conc"])
SCHEMA = "hls-audit/v1"


class AuditError(Exception):
    """A node of the tree cannot be audited / the tree cannot be
    walked. Source mode: fatal (exit 1). Package mode: the node is
    recorded fail-closed and the run still fails, after the report."""


# ---------------------------------------------------------------------------
# Shared audit core.
# ---------------------------------------------------------------------------

def _parse_file(abs_path):
    """Parse one .hls file exactly the way boot.py does (same
    tokenizer, same parser) — used for per-module fn attribution,
    which the merged program deliberately flattens away."""
    try:
        with open(abs_path, "rb") as f:
            src = f.read()
    except OSError as ex:
        raise AuditError("cannot read %s (%s)" % (abs_path, ex))
    try:
        return Parser(tokenize(src), src).parse_program()
    except (Exception, SystemExit) as ex:
        raise AuditError("parse error in %s: %s" % (abs_path, ex))


def _module_key(abs_path, repo_root):
    """Display key: repo-relative inside the repo, absolute outside
    (a git dependency checked out elsewhere is shown where it lives)."""
    ap = os.path.realpath(abs_path)
    rr = os.path.realpath(repo_root)
    if ap.startswith(rr + os.sep):
        return os.path.relpath(ap, rr).replace(os.sep, "/")
    return ap.replace(os.sep, "/")


def _classify(abs_path, repo_root):
    """toolchain (this repo's std/ + core/ — versioned with the
    compiler), dependency (resolved through HLS_PKG_DEPS, the
    .hls-pkg-deps farm `hls-pkg build` installs), or workspace
    (everything else — the code in your own tree)."""
    ap = os.path.realpath(abs_path)
    rr = os.path.realpath(repo_root)
    if ap.startswith(os.path.join(rr, "std") + os.sep) or \
       ap.startswith(os.path.join(rr, "core") + os.sep):
        return "toolchain"
    deps_dir = os.environ.get("HLS_PKG_DEPS")
    if deps_dir:
        dd = os.path.realpath(deps_dir)
        if ap.startswith(dd + os.sep):
            return "dependency"
    return "workspace"


def _intrinsic_effects(checker, fn_keys):
    """The effects a set of functions performs ITSELF: builtin calls
    in their bodies plus the declared effects of the externs they
    call. Everything else in a fixpoint set arrived through imports,
    and attribution must not charge it to the importer."""
    intr = set()
    for key in fn_keys:
        for callee in sorted(checker.edges.get(key, ())):
            if callee.startswith("b:"):
                intr |= BUILTIN_EFFECTS.get(callee[2:], set())
            else:
                callee_fn = checker.fns.get(callee)
                if callee_fn is not None and callee_fn.get("extern", False):
                    intr |= set(callee_fn["effects"])
    return intr


def _check_merged(entry_path):
    """load_program + check — the ONE authoritative fixpoint. Raises
    AuditError on any compile-level problem."""
    try:
        program = load_program(entry_path)
        return program, check(program)
    except (Exception, SystemExit) as ex:
        raise AuditError("unauditable: %s" % ex)


def _fn_keys_per_module(module_paths):
    """Parse each module separately: fn key -> owning module, plus the
    extern blocks per module. Duplicate fns across modules are a
    compile error in the merged check anyway; the mapping is
    well-defined for any program that audits."""
    fn_owner = {}
    module_fns = {}
    module_externs = {}
    for mp in module_paths:
        prog = _parse_file(mp)
        keys = list(prog["fns"].keys())
        for key in keys:
            if key in fn_owner and fn_owner[key] != mp:
                raise AuditError("duplicate function across modules: %s"
                                 % key)
            fn_owner[key] = mp
        module_fns[mp] = keys
        module_externs[mp] = [
            {"abi": blk["abi"],
             "fns": [d["name"] for d in blk["decls"]],
             "effects": sorted({e for d in blk["decls"]
                                for e in d["effects"]})}
            for blk in prog.get("externs", [])
        ]
    return fn_owner, module_fns, module_externs


def _walk_import_graph(entry_path):
    """The import DAG, resolved with boot.py's OWN resolver (identical
    semantics, including HLS_PKG_DEPS). Returns (edges, order):
    edges maps realpath -> sorted child realpaths, order is BFS from
    the entry. A module that cannot be found or parsed is fatal."""
    entry_abs = os.path.realpath(os.path.abspath(entry_path))
    edges, order, seen = {}, [], set()
    queue = [entry_abs]
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        order.append(cur)
        prog = _parse_file(cur)
        kids = []
        for imp in prog["imports"]:
            resolved = _resolve_import(imp["path"], cur)
            if resolved is None:
                raise AuditError("module not found: %s (imported by %s)"
                                 % (imp["path"], cur))
            kids.append(os.path.realpath(resolved))
            queue.append(kids[-1])
        edges[cur] = sorted(set(kids))
    return edges, order


def _chain_to_root(module, edges, root):
    """Shortest import chain root -> ... -> module (BFS parents)."""
    if module == root:
        return [root]
    parents, queue, seen = {}, [root], {root}
    while queue:
        cur = queue.pop(0)
        for kid in edges.get(cur, ()):
            if kid not in seen:
                seen.add(kid)
                parents[kid] = cur
                queue.append(kid)
    chain = [module]
    while chain[-1] != root:
        p = parents.get(chain[-1])
        if p is None:
            return [root, module]
        chain.append(p)
    chain.reverse()
    return chain


# ---------------------------------------------------------------------------
# Source mode.
# ---------------------------------------------------------------------------

def audit_source(entry_path, allow, repo_root=REPO_ROOT):
    """Build the supply-chain report for an entry file's import tree.
    Returns the report dict; raises AuditError when the tree cannot be
    audited at all (a broken tree is a broken build, not a finding)."""
    entry_abs = os.path.realpath(os.path.abspath(entry_path))
    if not os.path.isfile(entry_abs):
        raise AuditError("no such file: %s" % entry_path)
    edges, order = _walk_import_graph(entry_abs)
    _, module_fns, module_externs = _fn_keys_per_module(order)
    program, checker = _check_merged(entry_abs)
    computed = getattr(checker, "computed_effects", {})

    modules = []
    for mp in order:
        keys = module_fns.get(mp, [])
        surface = set()
        for k in keys:
            surface |= computed.get(k, set())
        modules.append({
            "key": _module_key(mp, repo_root),
            "kind": _classify(mp, repo_root),
            "fns": len(keys),
            "intrinsic": sorted(_intrinsic_effects(checker, keys)),
            "surface": sorted(surface),
            "root": mp == entry_abs,
        })
    key_of = {mp: _module_key(mp, repo_root)
              for mp, m in zip(order, modules)}

    program_surface = set()
    for m in modules:
        program_surface |= set(m["surface"])
    attribution = []
    for eff in sorted(program_surface):
        for mp, m in zip(order, modules):
            if eff not in m["intrinsic"]:
                continue
            chain = _chain_to_root(mp, edges, entry_abs)
            attribution.append({
                "effect": eff,
                "module": m["key"],
                "kind": m["kind"],
                "root_introduced": mp == entry_abs,
                "chain": [key_of[c] for c in chain],
            })

    violations = []
    if allow is not None:
        allowed = set(allow)
        # The gate covers the SUPPLY CHAIN: every introduction outside
        # the entry file. The root is your code (the same division
        # package mode draws with the root manifest).
        for att in attribution:
            if att["effect"] not in allowed and not att["root_introduced"]:
                violations.append({"effect": att["effect"],
                                   "module": att["module"],
                                   "chain": att["chain"]})

    extern_blocks = []
    for mp in order:
        for blk in module_externs.get(mp, []):
            extern_blocks.append({
                "module": _module_key(mp, repo_root),
                "abi": blk["abi"],
                "fns": blk["fns"],
                "effects": blk["effects"],
            })

    return {
        "schema": SCHEMA,
        "mode": "source",
        "root": _module_key(entry_abs, repo_root),
        "modules": modules,
        "edges": {key_of[a]: [key_of[b] for b in bs]
                  for a, bs in edges.items() if a in key_of},
        "extern_blocks": extern_blocks,
        "attribution": attribution,
        "program_surface": sorted(program_surface),
        "policy": {"allowed": sorted(allow) if allow is not None else None},
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# Package mode.
# ---------------------------------------------------------------------------

def _confined_join(base, rel, what):
    """Join + confine: relative only, no '..' segments, result stays
    inside base — the same rule boot.py applies to import paths."""
    if os.path.isabs(rel):
        raise AuditError("%s: path must be relative, got absolute: %s"
                         % (what, rel))
    parts = rel.replace("\\", "/").split("/")
    if ".." in parts:
        raise AuditError("%s: path must not contain '..' segments: %s"
                         % (what, rel))
    cand = os.path.normpath(os.path.join(base, rel))
    if cand != base and not cand.startswith(base + os.sep):
        raise AuditError("%s: path escapes the package directory: %s"
                         % (what, rel))
    return cand


def _resolve_dep(dep_name, source, declaring_dir, repo_root):
    """Resolve one dependency to (package_dir, content_path). The
    package dir is what we audit; the content path is what hls-pkg
    hashes into the lockfile (the dep's FILE for a single-file path
    dep, the directory otherwise) — drift must compare the SAME
    bytes the lock recorded. Package-relative FIRST (a manifest
    inside a package is that package's root view — the natural
    reading for nested deps), then repo-root-relative (the hls-pkg
    contract for top-level path deps). git deps go through hls-pkg's
    own validated resolver."""
    if not isinstance(source, dict):
        raise AuditError("dependency %s: source must be a table" % dep_name)
    if "git" in source:
        p = os.path.realpath(resolve_dependency(dep_name, source))
        return (os.path.dirname(p) if os.path.isfile(p) else p), p
    if "path" in source:
        rel = str(source["path"])
        for base in (declaring_dir, repo_root):
            cand = _confined_join(base, rel, "dependency %s" % dep_name)
            if os.path.isdir(cand):
                return os.path.realpath(cand), os.path.realpath(cand)
            if os.path.isfile(cand):
                full = os.path.realpath(cand)
                return os.path.dirname(full), full
        raise AuditError("dependency %s: path not found: %s"
                         % (dep_name, rel))
    raise AuditError("dependency %s has no git/path source" % dep_name)


def _sha256_tree(path):
    """The same content hash the lockfile records (file sha256, or
    hls-pkg's deterministic directory hash) — so drift means exactly
    what `hls-pkg verify` means."""
    if os.path.isfile(path):
        return sha256_file(path)
    from hpkg_cmds import _sha256_directory
    return _sha256_directory(path)


class _PkgNode(object):
    __slots__ = ("dir", "name", "version", "manifest", "deps", "parent",
                 "depth", "audit", "shared", "src_kind", "content_path")

    def __init__(self, dirname, name, version, manifest, depth):
        self.dir = dirname
        self.name = name
        self.version = version
        self.manifest = manifest
        self.deps = []      # list[_PkgNode], in manifest (sorted) order
        self.parent = None
        self.depth = depth
        self.audit = None
        self.shared = False
        self.src_kind = "root"  # how the parent named this package
        self.content_path = dirname  # what the lockfile hashes


def _walk_packages(pkg_dir, repo_root, stack, cache):
    """Depth-first walk of the package TREE. Each dep edge instantiates
    its own subtree (a diamond prints both paths — that is what the
    chain attribution needs); the audit result is cached per directory
    so a shared package is checked once. A directory on the current
    stack is a cycle: refused with the chain that closes it."""
    real = os.path.realpath(pkg_dir)
    for i, (d, nm) in enumerate(stack):
        if d == real:
            names = [x[1] for x in stack[i:]]
            raise AuditError("circular package dependency: %s"
                             % " -> ".join(names + [nm]))
    manifest_path = os.path.join(real, "hls-pkg.toml")
    if not os.path.isfile(manifest_path):
        raise AuditError("no hls-pkg.toml in %s" % real)
    try:
        manifest = parse_manifest(manifest_path)
    except (ValueError, OSError, FileNotFoundError) as ex:
        raise AuditError("manifest %s: %s" % (manifest_path, ex))
    pkg = manifest.get("package") or {}
    name = str(pkg.get("name") or os.path.basename(real))
    version = str(pkg.get("version") or "0.0.0")
    node = _PkgNode(real, name, version, manifest, len(stack))
    if real in cache:
        # Audited under an earlier parent: reuse the audit, keep the
        # subtree shallow (its own deps were walked there and the
        # chains through THIS path are drawn from the tree itself).
        node.audit = cache[real]
        node.shared = True
        return node
    stack.append((real, name))
    deps = manifest.get("dependencies") or {}
    if not isinstance(deps, dict):
        raise AuditError("package %s: [dependencies] section is corrupt"
                         % name)
    for dep_name in sorted(deps):
        dep_dir, dep_content = _resolve_dep(dep_name, deps[dep_name],
                                            real, repo_root)
        child = _walk_packages(dep_dir, repo_root, stack, cache)
        child.parent = node
        child.src_kind = "git" if "git" in (deps[dep_name] or {}) \
            else "path"
        child.content_path = dep_content
        node.deps.append(child)
    stack.pop()
    return node


def _audit_one_package(node):
    """Audit one package's entry through the real checker. A library
    entry (no main) goes through the wrapper contract hls-pkg lock
    uses: a generated wrapper next to the entry imports it and
    declares a pure main. The package's INTRINSIC effects count only
    functions defined in package-local files (its own imports and the
    toolchain modules it pulls in are shown, not charged)."""
    if node.audit is not None:
        return node.audit
    entry = str((node.manifest.get("package") or {}).get("entry")
                or "main.hls")
    entry_path = os.path.join(node.dir, entry)
    if not os.path.isfile(entry_path):
        raise AuditError("package %s: entry not found: %s"
                         % (node.name, entry))
    wrapper_used = False
    try:
        program, checker = _check_merged(entry_path)
    except AuditError as ex:
        if "missing main function" not in str(ex):
            raise AuditError("package %s unauditable: %s"
                             % (node.name, ex))
        wrapper_path = os.path.join(
            node.dir, ".hls-audit-wrapper-%d.hls" % os.getpid())
        try:
            with open(wrapper_path, "w") as f:
                f.write('# auto-generated by hls-audit\n')
                f.write('import "%s"\n' % entry)
                f.write('fn main() -> int pure { return 0 }\n')
            try:
                program, checker = _check_merged(wrapper_path)
                wrapper_used = True
            except AuditError as ex2:
                raise AuditError("package %s unauditable: %s"
                                 % (node.name, ex2))
            finally:
                try:
                    os.unlink(wrapper_path)
                except OSError:
                    pass
        except (OSError, IOError) as ex:
            raise AuditError("package %s unauditable: %s"
                             % (node.name, ex))
    computed = getattr(checker, "computed_effects", {})
    # Package-local files: the entry tree's modules under the package
    # dir. Toolchain / dependency modules are visible but not charged.
    all_keys = list(program["fns"].keys())
    # Re-derive the fn->file map: walk the entry's import tree (the
    # wrapper imports the same tree, so the map is identical).
    _, order = _walk_import_graph(entry_path)
    _, module_fns, _ = _fn_keys_per_module(order)
    local_keys = []
    for mp, keys in module_fns.items():
        rp = os.path.realpath(mp)
        if rp == node.dir or rp.startswith(node.dir + os.sep):
            local_keys.extend(keys)
    declared = set()
    surface = set()
    for key in all_keys:
        fn = program["fns"][key]
        declared |= set(fn["effects"])
        surface |= computed.get(key, set())
    intrinsic = _intrinsic_effects(checker, local_keys)
    node.audit = {
        "declared": sorted(declared),
        "surface": sorted(surface),
        "intrinsic": sorted(intrinsic),
        "wrapper": wrapper_used,
        "entry": entry,
        "fns": len(all_keys),
    }
    return node.audit


def _all_nodes(root):
    out = []

    def rec(n):
        out.append(n)
        for d in n.deps:
            rec(d)
    rec(root)
    return out


def _chain_names(node):
    chain = []
    while node is not None:
        chain.append(node.name)
        node = node.parent
    chain.reverse()
    return chain


def audit_package(root_dir, allow, repo_root=REPO_ROOT):
    """Build the supply-chain report for a package tree. Returns the
    report dict; the caller decides the exit code (violations, drift,
    unauditable)."""
    root_dir = os.path.realpath(os.path.abspath(root_dir))
    if not os.path.isdir(root_dir):
        raise AuditError("not a directory: %s" % root_dir)
    root = _walk_packages(root_dir, repo_root, [], {})
    nodes = _all_nodes(root)

    # Audit every UNIQUE package; a node that cannot be audited is
    # recorded fail-closed (full effect set) and named — never pure.
    cache = {}
    unauditable = []
    for node in nodes:
        if node.dir in cache:
            node.audit = cache[node.dir]
            continue
        try:
            _audit_one_package(node)
        except AuditError as ex:
            node.audit = {"declared": sorted(ALL_EFFECTS),
                          "surface": sorted(ALL_EFFECTS),
                          "intrinsic": sorted(ALL_EFFECTS),
                          "wrapper": False, "entry": None, "fns": 0,
                          "unauditable": str(ex)}
            unauditable.append({"package": node.name, "why": str(ex)})
        cache[node.dir] = node.audit

    # Lockfile drift: when a lockfile exists, every TOP-LEVEL dep's
    # recorded sha256 must still match the tree it names.
    lock_path = os.path.join(root_dir, "hls-pkg.lock")
    lockfile = "absent"
    drift = []
    if os.path.isfile(lock_path):
        lockfile = "present"
        try:
            with open(lock_path, "r") as f:
                lock = json.load(f)
        except (OSError, ValueError) as ex:
            raise AuditError("cannot read lockfile: %s" % ex)
        locked = {p.get("name"): p for p in (lock.get("packages") or [])
                  if isinstance(p, dict)}
        for child in root.deps:
            rec = locked.get(child.name)
            if rec is None:
                drift.append({"package": child.name,
                              "why": "in the manifest but not in the "
                                     "lockfile"})
                continue
            was = str(rec.get("sha256") or "")
            now = _sha256_tree(child.content_path)
            if now != was:
                drift.append({"package": child.name,
                              "why": "locked sha256 %s but the tree now "
                                     "hashes %s — re-lock"
                                     % (was[:12] or "<none>", now[:12])})

    # Attribution: an effect is introduced by every package that
    # performs it intrinsically; the chains follow the dep edges from
    # the root (a diamond prints one line per path — that is the
    # honest count).
    all_effects = set()
    for node in nodes:
        all_effects |= set(node.audit["surface"])
    attribution = []
    for eff in sorted(all_effects):
        for node in nodes:
            if node.audit.get("unauditable"):
                continue  # synthetic full-set surface — named separately
            if eff in node.audit["intrinsic"]:
                attribution.append({
                    "effect": eff,
                    "package": node.name,
                    "depth": node.depth,
                    "root_introduced": node is root,
                    "chain": _chain_names(node),
                })

    # Policy: the root manifest's [effects].allowed (or --allow) gates
    # every NON-ROOT package's surface. Unauditable nodes already fail
    # the run on their own — they are not double-counted here.
    if allow is not None:
        allowed, policy_source = set(allow), "--allow"
    else:
        eff_sec = root.manifest.get("effects") or {}
        raw = eff_sec.get("allowed") if isinstance(eff_sec, dict) else None
        if isinstance(raw, list):
            allowed = set(str(x) for x in raw)
            policy_source = "manifest"
        else:
            allowed, policy_source = None, None
    violations = []
    if allowed is not None:
        for node in nodes:
            if node is root or node.audit.get("unauditable"):
                continue
            for eff in node.audit["surface"]:
                if eff not in allowed:
                    violations.append({
                        "effect": eff,
                        "package": node.name,
                        "chain": _chain_names(node),
                    })

    def tree_of(node):
        return {"name": node.name, "version": node.version,
                "source": node.src_kind,
                "children": [tree_of(d) for d in node.deps]}

    def src_of(node):
        return node.src_kind

    return {
        "schema": SCHEMA,
        "mode": "package",
        "root": {"name": root.name, "version": root.version,
                 "dir": _module_key(root.dir, repo_root),
                 "surface": root.audit["surface"],
                 "intrinsic": root.audit["intrinsic"]},
        "lockfile": lockfile,
        "packages": [
            {"name": n.name, "version": n.version, "depth": n.depth,
             "source": src_of(n),
             "intrinsic": n.audit["intrinsic"],
             "surface": n.audit["surface"],
             "declared": n.audit["declared"],
             "entry": n.audit["entry"],
             "wrapper": n.audit["wrapper"],
             "unauditable": n.audit.get("unauditable")}
            for n in nodes
        ],
        "tree": tree_of(root),
        "attribution": attribution,
        "drift": drift,
        "unauditable": unauditable,
        "policy": {"allowed": sorted(allowed) if allowed is not None
                   else None,
                   "source": policy_source},
        "violations": violations,
        "program_surface": sorted(all_effects),
    }


# ---------------------------------------------------------------------------
# Rendering (text).
# ---------------------------------------------------------------------------

def _fmt(xs):
    return ", ".join(xs) if xs else "(none)"


def _tree_lines(tree, root_tag):
    lines = [("  %s %s  [%s]%s" % (tree["name"], tree["version"],
                                   tree["source"], root_tag))]

    def rec(children, prefix):
        for i, ch in enumerate(children):
            last = i == len(children) - 1
            lines.append("  %s%s%s %s  [%s]"
                         % (prefix, "`- " if last else "|- ",
                            ch["name"], ch["version"], ch["source"]))
            rec(ch["children"], prefix + ("   " if last else "|   "))
    rec(tree["children"], "")
    return lines


def render_source(r):
    out = ["== hls-audit: supply-chain effect report ==",
           "root: %s" % r["root"],
           "mode: source"]
    kinds = [m["kind"] for m in r["modules"]]
    n_edges = sum(len(v) for v in r["edges"].values())
    n_ext_fns = sum(len(b["fns"]) for b in r["extern_blocks"])
    out.append("modules: %d (%d toolchain, %d dependency, %d workspace) — "
               "edges: %d — extern decls: %d"
               % (len(r["modules"]),
                  kinds.count("toolchain"), kinds.count("dependency"),
                  kinds.count("workspace"), n_edges, n_ext_fns))
    out.append("")
    out.append("tree:")
    key2mod = {m["key"]: m for m in r["modules"]}
    edges = r["edges"]
    out.append("  %s  [%s] (this file)"
               % (r["root"], key2mod[r["root"]]["kind"]))

    def rec(key, prefix, seen):
        kids = edges.get(key, [])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            if kid in seen:
                out.append("  %s%s%s  (seen)" % (prefix,
                                                 "`- " if last else "|- ",
                                                 kid))
                continue
            out.append("  %s%s%s  [%s]" % (prefix,
                                           "`- " if last else "|- ",
                                           kid,
                                           key2mod[kid]["kind"]))
            rec(kid, prefix + ("   " if last else "|   "), seen | {kid})
    rec(r["root"], "", {r["root"]})
    out.append("")
    out.append("effect surface per module:")
    w = max([len(m["key"]) for m in r["modules"]] + [len("module")]) + 4
    out.append("  %s%s  %s" % ("module".ljust(w), "intrinsic".ljust(16),
                               "reachable (surface)"))
    for m in r["modules"]:
        out.append("  %s%s  %s" % (m["key"].ljust(w),
                                   _fmt(m["intrinsic"]).ljust(16),
                                   _fmt(m["surface"])))
    out.append("")
    out.append("attribution:")
    if not r["attribution"]:
        out.append("  (no effects anywhere in the tree — a pure supply "
                   "chain)")
    for att in r["attribution"]:
        out.append("  %s — introduced by %s [%s]"
                   % (att["effect"], att["module"], att["kind"]))
        out.append("       via: %s" % " -> ".join(att["chain"]))
    if r["extern_blocks"]:
        out.append("")
        out.append("ffi surface (trusted C/JS, outside the effect algebra):")
        for b in r["extern_blocks"]:
            out.append("  %s: extern \"%s\" { %s } declared %s"
                       % (b["module"], b["abi"], ", ".join(b["fns"]),
                          _fmt(b["effects"])))
    out.append("")
    out.append("totals:")
    out.append("  program effect surface: %s" % _fmt(r["program_surface"]))
    for kind in ("workspace", "dependency", "toolchain"):
        ks = sorted({a["effect"] for a in r["attribution"]
                     if a["kind"] == kind and not a["root_introduced"]})
        out.append("  introduced in %s: %s" % (kind, _fmt(ks)))
    out.append("")
    if r["policy"]["allowed"] is None:
        out.append("policy: (none — report only)")
        out.append("verdict: report only — pass --allow (or audit inside a "
                   "package with [effects].allowed) to gate the chain")
    else:
        out.append("policy: allowed = %s" % _fmt(r["policy"]["allowed"]))
        if r["violations"]:
            out.append("violations:")
            for v in r["violations"]:
                out.append("  %s (introduced by %s)"
                           % (v["effect"], v["module"]))
                out.append("       via: %s" % " -> ".join(v["chain"]))
            out.append("verdict: VIOLATED — %d effect(s) outside the "
                       "allow list" % len(r["violations"]))
        else:
            out.append("verdict: OK — the chain fits the allow list")
    return "\n".join(out)


def render_package(r):
    out = ["== hls-audit: supply-chain effect report ==",
           "root: %s %s (%s)" % (r["root"]["name"], r["root"]["version"],
                                 r["root"]["dir"]),
           "mode: package — lockfile: %s" % r["lockfile"]]
    depth_max = max([p["depth"] for p in r["packages"]] or [0])
    out.append("packages: %d (root included) — deepest chain: %d dep(s)"
               % (len(r["packages"]), depth_max))
    out.append("")
    out.append("tree:")
    out.extend(_tree_lines(r["tree"], " (this package)"))
    out.append("")
    out.append("effect surface per package:")
    seen_names = set()
    rows = [p for p in r["packages"]
            if not (p["name"] in seen_names or seen_names.add(p["name"]))]
    w = max([len(p["name"]) for p in rows] + [len("package")]) + 4
    out.append("  %s%s  %s" % ("package".ljust(w),
                               "intrinsic".ljust(16),
                               "reachable (surface)".ljust(22) + "declared"))
    for p in rows:
        note = "  (UNAUDITABLE — fail closed)" if p["unauditable"] else ""
        out.append("  %s%s  %s%s"
                   % (p["name"].ljust(w), _fmt(p["intrinsic"]).ljust(16),
                      _fmt(p["surface"]).ljust(22),
                      _fmt(p["declared"]) + note))
    out.append("")
    out.append("attribution:")
    if not r["attribution"]:
        out.append("  (no effects anywhere in the tree — a pure supply "
                   "chain)")
    for att in r["attribution"]:
        out.append("  %s — introduced by %s (depth %d)"
                   % (att["effect"], att["package"], att["depth"]))
        out.append("       via: %s" % " -> ".join(att["chain"]))
    out.append("")
    out.append("drift:")
    if not r["drift"]:
        out.append("  (none)" if r["lockfile"] == "present"
                   else "  (no lockfile — nothing to compare; run "
                        "`hls-pkg lock` to pin the tree)")
    for d in r["drift"]:
        out.append("  %s: %s" % (d["package"], d["why"]))
    if r["unauditable"]:
        out.append("")
        out.append("unauditable (fail closed):")
        for u in r["unauditable"]:
            out.append("  %s: %s" % (u["package"], u["why"]))
    out.append("")
    if r["unauditable"]:
        # A package we could not audit fails the run on its own —
        # with or without a policy (its surface is synthetic).
        if r["policy"]["allowed"] is not None:
            out.append("policy: allowed = %s (from %s)"
                       % (_fmt(r["policy"]["allowed"]),
                          r["policy"]["source"]))
            if r["violations"]:
                out.append("violations:")
                for v in r["violations"]:
                    out.append("  %s (introduced by %s)"
                               % (v["effect"], v["package"]))
                    out.append("       via: %s" % " -> ".join(v["chain"]))
            out.append("verdict: FAILED — %d effect(s) outside the "
                       "allowed set, %d package(s) unauditable"
                       % (len(r["violations"]), len(r["unauditable"])))
        else:
            out.append("policy: (none — the unauditable node fails the "
                       "run on its own)")
            out.append("verdict: UNAUDITABLE — %d package(s) could not "
                       "be audited (fail closed)"
                       % len(r["unauditable"]))
    elif r["policy"]["allowed"] is None:
        out.append("policy: (none — report only)")
        out.append("verdict: report only — add [effects].allowed to the "
                   "root manifest (or pass --allow) to gate the chain")
    else:
        out.append("policy: allowed = %s (from %s)"
                   % (_fmt(r["policy"]["allowed"]), r["policy"]["source"]))
        if r["violations"]:
            out.append("violations:")
            for v in r["violations"]:
                out.append("  %s (introduced by %s)"
                           % (v["effect"], v["package"]))
                out.append("       via: %s" % " -> ".join(v["chain"]))
            out.append("verdict: VIOLATED — %d effect(s) outside the "
                       "allowed set" % len(r["violations"]))
        else:
            out.append("verdict: OK — every package fits the allowed set")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _parse_allow(raw):
    if raw is None:
        return None
    items = [x.strip() for x in raw.split(",") if x.strip()]
    for x in items:
        if x not in ALL_EFFECTS:
            raise ValueError(
                "unknown effect in --allow: %s (known: %s)"
                % (x, ", ".join(ALL_EFFECTS)))
    return items


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-audit",
        description="Stage 103 supply-chain effect report (transitive).")
    ap.add_argument("entry", nargs="?", default=None,
                    help="entry .hls file (source mode)")
    ap.add_argument("--pkg", nargs="?", const=".", default=None,
                    metavar="DIR",
                    help="audit the package tree rooted at DIR "
                         "(default: cwd)")
    ap.add_argument("--allow", default=None, metavar="E1,E2,...",
                    help="effect allow-list; chain effects outside it "
                         "fail the run")
    ap.add_argument("--json", action="store_true",
                    help="print the report as JSON (schema hls-audit/v1)")
    args = ap.parse_args(argv)

    if args.pkg is None and args.entry is None:
        ap.print_usage(sys.stderr)
        sys.stderr.write("error: give an entry file (source mode) or "
                         "--pkg [DIR] (package mode)\n")
        return 2
    if args.pkg is not None and args.entry is not None:
        sys.stderr.write("error: <entry> and --pkg are mutually exclusive "
                         "(one mode per run)\n")
        return 2
    try:
        allow = _parse_allow(args.allow)
    except ValueError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 2

    try:
        if args.pkg is not None:
            report = audit_package(args.pkg, allow)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(render_package(report))
            failed = bool(report["violations"] or report["drift"]
                          or report["unauditable"])
            return 1 if failed else 0
        report = audit_source(args.entry, allow)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_source(report))
        return 1 if report["violations"] else 0
    except AuditError as ex:
        sys.stderr.write("hls-audit: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
