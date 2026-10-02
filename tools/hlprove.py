#!/usr/bin/env python3
"""hlprove — the Stage 17 proof assistant for Halis (HLS).

Usage:
  python3 tools/hlprove.py <file.hls> [--fast] [--smt] [--z3] [--cvc5]
                            [--suggest-invariants] [--infer-invariants]
                            [--shapes]

Modes:
  (default)  proof report — which panic checks the interval prover
             proved dead per contracted function (the SAME annotations
             the native codegen uses under `-O fast`).
  --smt      additionally write one SMT-LIB2 (.smt2) file per contracted
             function: a QF_LIA encoding of the contract (str lengths
             are abstracted to Int). The files are runnable by external
             z3 — the roadmap's "SMT solver z3 via a bridge generated
             from HLS".
  --z3       run z3 on each generated .smt2 (if a z3 binary is
             available) and report sat/unsat per query.
  --cvc5     Stage 99: the same bridge, decided by CVC5 instead —
             the cvc5 binary if present, else the cvc5 python module
             (pip install cvc5). Inference under --cvc5 reports its
             verdicts as cvc5-verified; --z3 and --cvc5 are mutually
             exclusive (one backend decides per run).
  --suggest-invariants
             scan every loop and suggest candidate invariants (loop
             header bounds for const for-ranges, the while condition as
             an invariant text, and the set of variables mutated in the
             body) — the automatic inference rule set from the roadmap.
  --infer-invariants
             Stage 97: SMT-based loop-invariant discovery. Every loop
             is summarised (entry facts, per-variable transition
             relation, condition), candidates are generated from
             templates, and an SMT solver decides initiation and
             preservation.
             Only solver-verified invariants are reported as inferred;
             failed candidates are reported with their failing
             obligation. With --smt, the verified obligations are also
             written as .smt2 files (one per loop). The deciding
             backend follows the mode flag: z3 by default, cvc5 under
             --cvc5 — both decide the SAME obligations, so a verdict
             must agree across them (the Stage 99 gate pins it).
  --shapes    Stage 100: the separation-logic fragment — heap shapes.
             Three reports per function: the heap FOOTPRINT (every
             list binding classified read/write over alias classes —
             assignment aliases, clone() is fresh, unknown provenance
             is the wildcard class), the SEPARATION verdicts
             (own(a) * own(b) — writes disjoint — proven from freshness
             evidence, refused with the reason otherwise: f(xs, xs)
             aliases the parameters, so separation is never assumed),
             and the cursor-loop SHAPE TRIPLES (lseg * cell * lseg):
             anchored on <cursor> op <root>.len() (or range(a,
             <root>.len())), an affine self-step, and a frame check
             with teeth — every write through the anchor's alias class
             must be the cursor cell and no push/pop may touch it (the
             check that justifies treating len as loop-invariant in the
             obligations). Initiation and preservation are decided by
             the same backend as the invariants (z3, or cvc5 under
             --cvc5); with no solver the candidates are reported
             UNVERIFIED and nothing is claimed. Analysis-only: no
             compilation unit changes, interpreter and native binaries
             agree byte for byte.
  --sidechannel
             Stage 101: the cryptographic side-channel analysis. The
             #[secrets(...)] parameters are the taint roots; the pass
             tracks the marking through the bodies (assignment,
             arithmetic, calls — interprocedurally, by fixpoint) and
             reports every SINK where a secret becomes observable:
             branch / match on a secret-derived condition, memory
             access with a secret-dependent address (index, slice,
             map key), secret-decided loop bounds, and division/
             modulo with a secret operand. .len() on a secret value
             is public by policy — every use is counted and printed
             so the report states its assumption. Data-only flow
             (store, return, a call keeping the value internal) is
             never a leak — that is the point of the sink list.
  --consttime
             Stage 102: the constant-time VERIFIER. Every fn marked
             #[ct] claims to be constant-time over every secret that
             reaches it; the claim is proven, not trusted. Each claim
             is discharged in its OWN taint universe — only that fn's
             #[secrets(...)] parameters are roots, the rest of the
             program starts public — so a leak in an unrelated marked
             fn can never violate a claim it has no part in. The
             verdict is per claim: VERIFIED (no secret-derived branch,
             address, loop bound or division anywhere the secrets
             reach — own body included, transitively) or VIOLATED,
             with the sink lines, the one-line why, and the incoming
             chain (caller -> callee, the parameter each hop travels
             as) so the violation is attributable. A violation makes
             the run exit 1 — the verifier has teeth the Stage 101
             analysis deliberately lacks.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boot.boot import load_program            # noqa: E402
from boot.checker import check                # noqa: E402
from boot.lexer import HLError                # noqa: E402
from boot import proof as _proof             # noqa: E402


def proof_report(program, checker):
    """Count the elision annotations per function (mirrors what the
    native codegen elides under -O fast)."""
    lines = []
    totals = {"ovf": 0, "div": 0, "bnd": 0}

    def walk(e):
        if not isinstance(e, dict):
            return
        if e.get("ovf_safe"):
            totals["ovf"] += 1
        if e.get("div_safe"):
            totals["div"] += 1
        if e.get("bnd_safe"):
            totals["bnd"] += 1
        for key in ("l", "r", "e"):
            walk(e.get(key))
        for a in (e.get("args") or []):
            walk(a)
        if e.get("k") == "method":
            walk(e.get("target"))
        if e.get("k") == "index":
            walk(e.get("target"))
            walk(e.get("idx"))
        if e.get("k") == "field":
            walk(e.get("target"))
        if e.get("k") == "match":
            # Deep-scan-10: the scrutinee expression was never walked, so
            # proven checks inside it were under-reported.
            walk(e.get("scrut"))
        for arm in (e.get("arms") or []):
            walk(arm.get("body"))
        if e.get("k") in ("listlit", "structlit", "enumlit"):
            # Deep-scan-10: literal items/fields were never walked.
            for a in (e.get("items") or []):
                walk(a)
            for _, fe in (e.get("fields") or []):
                walk(fe)

    def walk_stmts(stmts):
        for s in stmts or []:
            if not isinstance(s, dict):
                continue
            for key in ("value", "cond", "iter", "e"):
                walk(s.get(key))
            tgt = s.get("target")
            if isinstance(tgt, dict):
                walk(tgt.get("idx"))
            for bkey in ("body", "then", "els"):
                walk_stmts(s.get(bkey))

    for key, fn in program["fns"].items():
        if fn.get("requires") is None:
            continue
        before = dict(totals)
        walk_stmts(fn.get("body"))
        d = {k: totals[k] - before[k] for k in totals}
        facts = checker.proof_facts.get(key, {})
        req = fn.get("requires")
        lines.append((key, d, facts, req))
    return lines, totals


def expr_to_text(e):
    """Best-effort textual rendering of a contract expression."""
    if not isinstance(e, dict):
        return "?"
    k = e.get("k")
    if k == "int" or k == "float" or k == "bool":
        return str(e.get("v"))
    if k == "str":
        return '"%s"' % e.get("v", b"").decode("latin-1")
    if k == "ident":
        return e.get("name", "?")
    if k == "un":
        return "%s(%s)" % (e.get("op"), expr_to_text(e.get("e")))
    if k == "bin":
        return "(%s %s %s)" % (expr_to_text(e.get("l")), e.get("op"),
                               expr_to_text(e.get("r")))
    if k == "call":
        args = ", ".join(expr_to_text(a) for a in (e.get("args") or []))
        return "%s(%s)" % (e.get("name", "?"), args)
    if k == "method":
        return "%s.%s()" % (expr_to_text(e.get("target")), e.get("name"))
    return "?"


def gen_smt(program, out_dir):
    """Generate one .smt2 file per contracted function. Returns the list
    of (fn_key, path)."""
    results = []
    for key, fn in program["fns"].items():
        req = fn.get("requires")
        ens = fn.get("ensures")
        if req is None and ens is None:
            continue
        vars_int = set()
        vars_str = set()
        for pn, pt, _ in fn["params"]:
            if pt == "int":
                vars_int.add(pn)
            elif pt == "str":
                vars_str.add(pn)

        def scan(e):
            if not isinstance(e, dict):
                return
            if e.get("k") == "ident":
                n = e.get("name")
                if n in vars_str or n in vars_int or n == "result":
                    pass
            for sub in (e.get("l"), e.get("r"), e.get("e")):
                scan(sub)
            for a in (e.get("args") or []):
                scan(a)

        scan(req)
        scan(ens)
        # Deep-scan-10 fix: the result sort follows the return type (int
        # -> Int, bool -> Bool, anything else -> no declaration, and
        # ensures queries referencing result are skipped for those). It
        # used to be Int-or-nothing, so a bool contract asserting
        # `result` referenced an undeclared constant (a z3 error).
        ret = fn.get("ret")
        result_int = True if ret == "int" else (False if ret == "bool"
                                                else None)
        lines = _proof.smt_prelude(sorted(vars_int), sorted(vars_str),
                                   result_int)
        try:
            if req is not None:
                req_smt = _proof.smt_of_expr(req, vars_int, vars_str)
                # Query 1: is the requires satisfiable? (unsat = the
                # contract is vacuous — NO valid input exists.)
                lines.append("; (check-sat) requires satisfiability:"
                             " unsat => the contract is vacuous")
                lines.append("(assert %s)" % req_smt)
                lines.append("(check-sat)")
                # Open the second segment only when a second query is
                # actually coming. A requires-only contract (or one
                # whose result has no QF_LIA encoding) used to leave a
                # trailing prelude-only segment with no (check-sat):
                # the z3 binary ignored it, the module-based runners
                # synthesised a verdict for it ("ensures: sat" from an
                # empty assertion set, "ensures: ?" under cvc5), and
                # the two backends' reports stopped matching.
                if ens is not None and result_int is not None:
                    lines.append("(reset)")
                    lines.extend(_proof.smt_prelude(sorted(vars_int),
                                                    sorted(vars_str),
                                                    result_int))
            if ens is not None:
                # Deep-scan-10 fix: an ENSURES-ONLY contract used to emit
                # declarations and NO query at all. Query 2 is now
                # emitted with or without a requires (vacuity check:
                # requires && !ensures, or just !ensures).
                if result_int is not None:
                    ens_smt = _proof.smt_of_expr(ens, vars_int, vars_str)
                    if req is not None:
                        req_smt2 = _proof.smt_of_expr(req, vars_int,
                                                      vars_str)
                        lines.append("; (check-sat) requires & !ensures:"
                                     " unsat => ensures is implied by"
                                     " requires")
                        lines.append("(assert %s)" % req_smt2)
                    else:
                        lines.append("; (check-sat) !ensures:"
                                     " unsat => ensures always holds")
                    lines.append("(assert (not %s))" % ens_smt)
                    lines.append("(check-sat)")
                else:
                    lines.append("; (ensures query skipped: result type"
                                 " %s has no QF_LIA encoding)" % ret)
        except ValueError as ex:
            lines.append("; unsupported contract shape for SMT: %s" % ex)
        path = os.path.join(out_dir, "%s.smt2" % key.replace(".", "_"))
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        results.append((key, path))
    return results


def _z3_verdicts_py(path):
    """Run the .smt2 through the z3 PYTHON module (z3-solver package)
    and return the list of check-sat verdicts. Returns None if the
    module is unavailable. Deep-scan-10 (Stage-17 perfection): --z3 used
    to require a z3 BINARY on PATH; the module fallback makes the bridge
    usable everywhere the package is installed."""
    # Deep-scan-15 cleanup: the previous code had TWO imports —
    # `import z3` (just to test importability) followed
    # by `import z3 as z` (the actual alias used below). The first
    # import is redundant: if z3 doesn't exist, both lines raise
    # ImportError; if it does, both succeed (Python caches modules
    # in sys.modules). Collapse to a single `import z3 as z`.
    try:
        import z3 as z
    except ImportError:
        return None
    # Deep-scan-13 fix: close the SMT transcript file deterministically
    # (the old bare open() relied on CPython's refcount GC — a resource
    # leak on PyPy and a ResourceWarning under -W error).
    with open(path) as f:
        text = f.read()
    # Split on (reset): each segment is an independent set of commands.
    segments = [seg.strip() for seg in text.split("(reset)") if seg.strip()]
    verdicts = []
    for seg in segments:
        solver = z.Solver()
        try:
            solver.from_string(seg)
        except Exception:
            verdicts.append("?")
            continue
        verdicts.append(str(solver.check()))
    return verdicts


def run_z3(smt_files):
    """Run z3 on each file: the z3 binary if present, else the z3 python
    module (z3-solver). Reports EVERY check-sat verdict in the file
    (deep-scan-10: only the last line was reported, so the vacuity
    verdict of a two-query file was silently dropped)."""
    for key, path in smt_files:
        verdicts = None
        try:
            proc = subprocess.run(["z3", path], capture_output=True,
                                  text=True, timeout=10)
            if proc.returncode == 0:
                out = (proc.stdout or "").strip().splitlines()
                verdicts = [ln for ln in out if ln in ("sat", "unsat",
                                                       "unknown")]
                if not verdicts:
                    verdicts = ["?"]
            else:
                verdicts = None
        except FileNotFoundError:
            verdicts = None
        except subprocess.TimeoutExpired:
            print("    %-28s z3: TIMEOUT" % key)
            continue
        if verdicts is None:
            verdicts = _z3_verdicts_py(path)
        if verdicts is None:
            print("    %-28s z3: neither the z3 binary nor the z3-solver"
                  " python package is available (install one, or use"
                  " --smt and inspect the .smt2 yourself)" % key)
            return
        labels = ["vacuity", "ensures"]
        disp = ", ".join("%s: %s" % (labels[i] if i < len(labels) else "q%d" % i, v)
                         for i, v in enumerate(verdicts))
        print("    %-28s z3: %s" % (key, disp))


# ---------------------------------------------------------------------------
# Stage 99 (v0.118.0-alpha): the CVC5 SMT backend — hlprove --cvc5.
#
# The same bridge, the same obligations, a second decider. CVC5 is the
# independent re-implementation the verification phase wants: whatever
# z3 certifies unsat, cvc5 must certify unsat too, or the encoding (not
# a solver quirk) is the bug. The plumbing mirrors the z3 pattern
# exactly — the binary first, the python module as the fallback, and
# NEITHER exists → nothing is claimed, the report says what to install.
# ---------------------------------------------------------------------------

def _cvc5_binary_decide(lines):
    """One obligation through a cvc5 BINARY (temp file — the stdin `-`
    convention is not portable across cvc5 builds). `-q` keeps the
    stderr chatter (the missing set-logic notice) out of the way; the
    verdicts land on stdout one per check-sat. Returns the verdict
    string or None when no binary is on PATH."""
    import tempfile
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".smt2",
                                         delete=False) as tf:
            tf.write("\n".join(lines) + "\n")
            tmp = tf.name
    except OSError:
        return None
    try:
        proc = subprocess.run(["cvc5", "--lang", "smt2", "-q", tmp],
                              capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "timeout"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        return "?"
    out = [ln.strip() for ln in (proc.stdout or "").splitlines()
           if ln.strip() in ("sat", "unsat", "unknown")]
    return out[0] if out else "?"


def _cvc5_module_decide(lines):
    """One obligation through the cvc5 PYTHON module (the `cvc5`
    package: pip install cvc5). The module ships no file runner, so the
    segment is executed command by command through an InputParser —
    each parsed command is invoked on the solver and the parser's
    symbol manager (the declared constants and the defined helpers must
    be visible to the commands parsed after them) — and the result of
    the final (check-sat) is the verdict. Returns the verdict string or
    None when the module is unavailable."""
    try:
        import cvc5
    except ImportError:
        return None
    text = "\n".join(lines)
    # The cvc5 core writes its notices (the missing set-logic hint,
    # most often) straight to fd 2 — a Python-level redirect does not
    # see them. Park fd 2 for the duration of the decide so the tool's
    # stderr stays reserved for hlprove's own diagnostics.
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        tm = cvc5.TermManager()
        solver = cvc5.Solver(tm)
        try:
            parser = cvc5.InputParser(solver)
        except (TypeError, RuntimeError):
            parser = cvc5.InputParser(tm, solver)
        sm = parser.getSymbolManager()
        lang = getattr(cvc5.InputLanguage, "SMT_LIB_2_6",
                       getattr(cvc5.InputLanguage, "SMT_LIB_V2", None))
        if lang is None:
            return "?"
        parser.setStringInput(lang, text, "hlprove")
        last = None
        while not parser.done():
            cmd = parser.nextCommand()
            if cmd.isNull():
                break
            try:
                last = cmd.invoke(solver, sm)
            except TypeError:
                last = cmd.invoke(solver)
        verdict = (last or "").strip()
        return verdict if verdict in ("sat", "unsat", "unknown") else "?"
    except Exception:
        return "?"
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


def _cvc5_verdicts_py(path):
    """Run the .smt2 through the cvc5 PYTHON module and return the list
    of check-sat verdicts — one per (reset)-separated segment, the same
    convention as the z3 module fallback. Returns None if the module is
    unavailable."""
    try:
        import cvc5  # noqa: F401
    except ImportError:
        return None
    with open(path) as f:
        text = f.read()
    segments = [seg.strip() for seg in text.split("(reset)") if seg.strip()]
    return [_cvc5_module_decide(seg.splitlines()) for seg in segments]


def run_cvc5(smt_files):
    """Run cvc5 on each file: the cvc5 binary if present, else the cvc5
    python module — the exact reporting contract of run_z3, so the two
    backends are interchangeable on the same .smt2."""
    for key, path in smt_files:
        verdicts = None
        try:
            proc = subprocess.run(["cvc5", "--lang", "smt2", "-q", path],
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                out = (proc.stdout or "").strip().splitlines()
                verdicts = [ln for ln in out if ln in ("sat", "unsat",
                                                       "unknown")]
                if not verdicts:
                    verdicts = ["?"]
            else:
                verdicts = None
        except FileNotFoundError:
            verdicts = None
        except subprocess.TimeoutExpired:
            print("    %-28s cvc5: TIMEOUT" % key)
            continue
        if verdicts is None:
            verdicts = _cvc5_verdicts_py(path)
        if verdicts is None:
            print("    %-28s cvc5: neither the cvc5 binary nor the cvc5"
                  " python package is available (install one, or use"
                  " --smt and inspect the .smt2 yourself)" % key)
            return
        labels = ["vacuity", "ensures"]
        disp = ", ".join("%s: %s" % (labels[i] if i < len(labels) else "q%d" % i, v)
                         for i, v in enumerate(verdicts))
        print("    %-28s cvc5: %s" % (key, disp))


def make_cvc5_decider():
    """The Stage 99 decider: the cvc5 binary first, then the cvc5
    python module — or None when neither exists (nothing is then
    claimed as inferred; the report says so)."""
    def decide(lines):
        v = _cvc5_binary_decide(lines)
        if v is not None:
            return v
        return _cvc5_module_decide(lines)

    if _cvc5_binary_decide(["(check-sat)"]) is not None:
        return decide
    if _cvc5_module_decide(["(check-sat)"]) is not None:
        return decide
    return None


def shapes_report(program, want_smt, out_dir, decider=None,
                  solver_name="z3"):
    """Stage 100: the heap-shape report. Footprints, separation
    verdicts and loop-shape triples per function; the decider decides
    every frame-clean candidate's two obligations (or nothing is
    claimed when no solver exists). Returns the summary counts."""
    fs_list = _proof.collect_shapes(program)
    # verify_shapes mutates each loop's status/segments in place; the
    # per-function segment map it returns belongs to the --smt dump of
    # the shapes pipeline, which this report path does not produce.
    _proof.verify_shapes(fs_list, decider)
    proven = refused = unverified = 0
    sep_ok = sep_no = 0
    print("  Heap shapes (the separation-logic fragment):")
    anything = False
    for fs in fs_list:
        # ---- footprint -------------------------------------------------
        root_kind = {}
        for cid, site in fs.writes:
            rep = fs.classes.repr_of(cid)
            root_kind[rep] = "W"
        for cid in fs.reads:
            rep = fs.classes.repr_of(cid)
            root_kind.setdefault(rep, "R")
        if not root_kind:
            continue
        anything = True
        print("  %s:" % fs.fn_key)
        fp = ", ".join("%s:%s" % (r, root_kind[r])
                       for r in sorted(root_kind))
        print("    footprint: %s (over alias classes)" % fp)
        # ---- separation -------------------------------------------------
        if not fs.separation:
            print("    separation: single writer — trivially separated")
        else:
            for ra, rb, ok, why in fs.separation:
                sep_ok += 1 if ok else 0
                sep_no += 0 if ok else 1
                print("    separation: own(%s) * own(%s) %s — %s"
                      % (ra, rb, "PROVEN" if ok else "REFUSED", why))
        # ---- loop shapes -------------------------------------------------
        for ls in fs.loops:
            if ls.status == "proven":
                proven += 1
            elif ls.status == "unverified":
                unverified += 1
            else:
                refused += 1
            print("    line %d: %s" % (ls.line, ls.header))
            if ls.root is None:
                print("      no heap anchor: %s" % ls.cond_reason)
                continue
            if ls.frame_reasons and ls.status != "proven":
                for why in ls.frame_reasons:
                    print("      frame REFUSED: %s" % why)
                continue
            print("      frame: every write is %s[%s]; len(%s) fixed "
                  "through the body" % (ls.root, ls.cursor, ls.root))
            if ls.status == "proven":
                print("      shape PROVEN (%s): %s" % (solver_name,
                                                       ls.shape_text))
                print("      accesses in bounds by shape: %d of %d"
                      % (ls.access_safe, ls.access_total))
                if ls.access_blocked:
                    print("      accesses NOT claimed: %d (after the "
                          "cursor update the cursor may sit at len)"
                          % ls.access_blocked)
            elif ls.status == "unverified":
                print("      shape CANDIDATE (unverified — no solver "
                      "installed, nothing claimed): %s"
                      % _proof._shape_of_loop_text(ls))
            else:
                if ls.solver_reason:
                    print("      shape REFUSED (obligation not "
                          "discharged): %s" % ls.solver_reason)
                else:
                    for why in ls.frame_reasons:
                        print("      shape REFUSED: %s" % why)
                if ls.access_blocked:
                    print("      accesses NOT claimed: %d of %d (after "
                          "the cursor update the cursor may sit at len)"
                          % (ls.access_blocked, ls.access_total))
                elif ls.access_safe < ls.access_total:
                    print("      accesses NOT claimed: %d of %d"
                          % (ls.access_total - ls.access_safe,
                             ls.access_total))
            if want_smt and ls.segments:
                fname = "%s__L%d.shape.smt2" % (
                    fs.fn_key.replace(".", "_"), ls.line)
                fpath = os.path.join(out_dir, fname)
                with open(fpath, "w") as f:
                    f.write(_proof.smt_program_text(
                        "; Halis heap-shape obligations — %s:%d %s\n"
                        "; verified shape: %s"
                        % (fs.fn_key, ls.line, ls.header, ls.shape_text),
                        ls.segments))
                print("      obligations -> %s" % fpath)
    if not anything:
        print("    (no list bindings — nothing for the fragment to say)")
    print("  SHAPES TOTAL: %d loops proven, %d refused, %d unverified;"
          " %d separation pairs proven, %d refused"
          % (proven, refused, unverified, sep_ok, sep_no))
    return proven, refused, unverified


def sidechannel_report(program, checker):
    """Stage 101: the side-channel audit. Prints the per-fn findings
    (kind, line, the construct as written) plus the policy notes and
    the incoming secret flows; returns the summary counts."""
    reports = _proof.analyze_sidechannel(program)
    involved = sorted(reports.values(),
                      key=lambda r: (not r.roots, r.fn_key))
    if not involved:
        print("    (no #[secrets(...)] annotation — nothing to audit; "
              "mark the secret parameters of the crypto code)")
        return 0, 0, 0, 0, 0
    counts = {k: 0 for k in _proof.SC_KIND_ORDER}
    leak_fns = 0
    for rep in involved:
        print("  %s:" % rep.fn_key)
        if rep.roots:
            print("    secret roots: %s" % ", ".join(rep.roots))
        extra = [p for p in rep.secret_params if p not in rep.roots]
        if extra:
            print("    secret by propagation: %s" % ", ".join(extra))
        for caller, param, line in rep.incoming:
            print("    incoming: '%s' passes a secret '%s' from %s "
                  "(line %d)" % (param, param, caller, line))
        for f in rep.findings:
            counts[f.kind] += 1
            print("    line %d: %s — `%s`" % (f.line, f.kind.upper(),
                                              f.text))
            print("      %s" % f.why)
        if rep.length_uses:
            print("    .len() on a secret value: %d use(s) — public by "
                  "policy" % rep.length_uses)
        if rep.findings:
            leak_fns += 1
        else:
            print("    CLEAN: no secret-dependent control flow, "
                  "addressing or division")
    total = sum(counts.values())
    print("  SIDECHANNEL TOTAL: %d leaks — %d branch, %d index, "
          "%d loop-bound, %d division — in %d of %d secret-handling "
          "fns"
          % (total, counts["branch"], counts["index"],
             counts["loop-bound"], counts["division"], leak_fns,
             len(involved)))
    return (total, counts["branch"], counts["index"],
            counts["loop-bound"], counts["division"])


def consttime_report(program, checker):
    """Stage 102: the constant-time verifier. One taint universe per
    #[ct] claim; VERIFIED / VIOLATED per claim with the sink lines,
    the whys and the incoming chains. Returns the violated count (the
    CLI exits 1 when it is nonzero — the verifier has teeth)."""
    claims = _proof.analyze_consttime(program)
    if not claims:
        print("    (no #[ct] claims — nothing to verify; mark the fns "
              "that must be constant-time with #[ct] and name their "
              "secrets with #[secrets(...)])")
        return 0
    verified = 0
    violated = 0
    for c in claims:
        print("  %s:" % c.fn_key)
        print("    claim: ct over secrets %s" % ", ".join(c.secrets))
        print("    secret reach: %s (%d fn%s)"
              % (", ".join(c.reach), len(c.reach),
                 "" if len(c.reach) == 1 else "s"))
        if c.length_uses:
            print("    policy: .len() on a secret value — %d use(s), "
                  "public by policy" % c.length_uses)
        if c.verified:
            verified += 1
            print("    VERIFIED: no secret-derived branch, address, "
                  "loop bound or division anywhere the secrets reach")
        else:
            violated += 1
            print("    VIOLATED: %d sink(s) reachable from the claim's "
                  "secrets" % len(c.findings))
            for wkey, f in c.findings:
                where = "" if wkey == c.fn_key else " (inside %s)" % wkey
                print("    line %d: %s — `%s`%s"
                      % (f.line, f.kind.upper(), f.text, where))
                print("      why: %s" % f.why)
                chain = c.chains.get((wkey, f.kind, f.line))
                if chain is not None:
                    print("      chain: %s" % _ct_chain_text(c, chain))
    print("  CT TOTAL: %d verified, %d violated of %d claims"
          % (verified, violated, len(claims)))
    return violated


def _ct_chain_text(claim, chain):
    """The chain as printed: the claim fn, then one hop per flow edge
    (the callee, the parameter the secret travels as, the call line).
    An empty chain is a sink in the claim's own body."""
    if not chain:
        return "%s (the claim's own body)" % claim.fn_key
    hops = [claim.fn_key]
    for callee, param, line in chain:
        hops.append("%s (%s, line %d)" % (callee, param, line))
    return " -> ".join(hops)


def suggest_invariants(program):
    """Loop-invariant suggestions (the roadmap's automatic inference
    rule set): const-bound for loops get exact bounds; while loops get
    their condition as a candidate invariant; both list the variables
    mutated in the body."""
    print("  Loop invariant suggestions:")
    found = 0

    def scan_stmts(stmts, fn_key):
        nonlocal found
        for s in stmts or []:
            if not isinstance(s, dict):
                continue
            k = s.get("k")
            if k == "for":
                it = s.get("iter")
                if isinstance(it, dict) and it.get("name") == "range":
                    args = it.get("args") or []
                    if len(args) == 2 and all(
                            isinstance(a, dict) and a.get("k") == "int"
                            for a in args):
                        lo = args[0].get("v")
                        hi = args[1].get("v")
                        print("    %s: for %s in range(%d, %d)"
                              % (fn_key, s.get("var"), lo, hi))
                        print("        suggest: %d <= %s < %d "
                              "(loop variable is monotonic)"
                              % (lo, s.get("var"), hi))
                        found += 1
                        scan_stmts(s.get("body"), fn_key)
                        continue
                print("    %s: for %s in <non-const iterable>"
                      % (fn_key, s.get("var")))
                print("        suggest: 0 <= %s < len(iterable_at_entry)"
                      % s.get("var"))
                found += 1
            elif k == "while":
                cond_txt = expr_to_text(s.get("cond"))
                print("    %s: while %s" % (fn_key, cond_txt))
                print("        suggest: the condition itself holds at "
                      "the top of every iteration")
                found += 1
            for bkey in ("body", "then", "els"):
                scan_stmts(s.get(bkey), fn_key)

    for key, fn in program["fns"].items():
        scan_stmts(fn.get("body"), key)
    if not found:
        print("    (no loops found)")


def _z3_binary_decide(lines):
    """One obligation through a z3 BINARY (temp file — the stdin `-`
    convention is not portable across z3 builds). Returns the verdict
    string or None when no binary is on PATH."""
    import tempfile
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".smt2",
                                         delete=False) as tf:
            tf.write("\n".join(lines) + "\n")
            tmp = tf.name
    except OSError:
        return None
    try:
        proc = subprocess.run(["z3", tmp], capture_output=True,
                              text=True, timeout=10)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "timeout"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode != 0:
        return "?"
    out = [ln.strip() for ln in (proc.stdout or "").splitlines()
           if ln.strip() in ("sat", "unsat", "unknown")]
    return out[0] if out else "?"


def _z3_module_decide(lines):
    """One obligation through the z3 PYTHON module. Returns the verdict
    string or None when the module is unavailable."""
    try:
        import z3 as z
    except ImportError:
        return None
    solver = z.Solver()
    try:
        solver.from_string("\n".join(lines))
    except Exception:
        return "?"
    return str(solver.check())


def make_decider():
    """A decide(lines) -> verdict callable, z3 binary first, then the
    z3-solver python module — or None when neither exists (nothing is
    then claimed as inferred; the report says so)."""
    def decide(lines):
        v = _z3_binary_decide(lines)
        if v is not None:
            return v
        return _z3_module_decide(lines)

    if _z3_binary_decide(["(check-sat)"]) is not None:
        return decide
    if _z3_module_decide(["(check-sat)"]) is not None:
        return decide
    return None


def infer_invariants(program, want_smt, out_dir, decider=None,
                     solver_name="z3"):
    """Stage 97: the SMT-based invariant inference pipeline. Returns an
    exit-visible summary (accepted/rejected counts). Stage 99: the
    decider and its printed name arrive as parameters — the SAME
    pipeline runs under either backend, so the verdicts (and the whole
    report modulo the solver's name) must not change with it."""
    loops = _proof.infer_loops(program)
    if decider is None:
        decider = make_decider()
    accepted = 0
    rejected = 0
    if not loops:
        print("  No loops found.")
        return 0, 0
    for loop in loops:
        print("  %s:%d: %s" % (loop.fn_key, loop.line, loop.header))
        carried = [loop.transition_text(v) for v in sorted(loop.carried)]
        unmod = ["%s (%s)" % (v, loop.unmodeled[v])
                 for v in sorted(loop.unmodeled)]
        if carried:
            print("    loop-carried: %s" % ", ".join(carried))
        if unmod:
            print("    unmodeled: %s" % ", ".join(unmod))
        if loop.inv_vars:
            print("    invariant over: %s" % ", ".join(sorted(loop.inv_vars)))
        if loop.note:
            print("    note: %s" % loop.note)
        if not loop.cands:
            print("    no candidates (nothing about this loop can be "
                  "discharged)")
            if decider is None:
                continue
        if decider is None:
            print("    candidates: %s" % ", ".join(c.text for c in loop.cands))
            if solver_name == "cvc5":
                print("    (no cvc5 binary and no cvc5 module — candidates "
                      "are NOT verified, nothing is claimed as inferred; "
                      "install a cvc5 binary or pip install cvc5)")
            else:
                print("    (no z3 binary and no z3-solver module — candidates "
                      "are NOT verified, nothing is claimed as inferred; "
                      "install z3 or pip install z3-solver)")
            rejected += len(loop.cands)
            continue
        segs = _proof.verify_loop(loop, decider)
        accepted += len(loop.accepted)
        rejected += len(loop.rejected)
        if loop.accepted:
            print("    inferred (%s-verified inductive):" % solver_name)
            for c in loop.accepted:
                print("      %s   (%s)" % (c.text, c.tag))
        else:
            print("    inferred: (none)")
        for c, why in loop.rejected:
            print("    rejected: %s (%s obligation not discharged)"
                  % (c.text, why))
        if want_smt and segs:
            fname = "%s__L%d.smt2" % (loop.fn_key.replace(".", "_"),
                                      loop.line)
            fpath = os.path.join(out_dir, fname)
            with open(fpath, "w") as f:
                f.write(_proof.smt_program_text(
                    "; Halis loop-invariant obligations — %s:%d %s\n"
                    "; verified inductive set: %s"
                    % (loop.fn_key, loop.line, loop.header,
                       ", ".join(c.text for c in loop.accepted) or "(none)"),
                    segs))
            print("    obligations -> %s" % fpath)
        print()
    return accepted, rejected


def main():
    args = sys.argv[1:]
    want_cvc5 = "--cvc5" in args
    want_z3 = "--z3" in args
    # Stage 99: one backend decides per run. A run asking for BOTH is a
    # caller error — silently picking one would make the report's
    # solver column a coin flip.
    if want_cvc5 and want_z3:
        sys.stderr.write("hlprove: --z3 and --cvc5 name different backends"
                         " — pick one\n")
        return 2
    # Deep-scan-10: --z3 without --smt silently did nothing; it now
    # implies it. Stage 99: --cvc5 implies it for the same reason.
    want_smt = "--smt" in args or want_z3 or want_cvc5
    want_inv = "--suggest-invariants" in args
    want_infer = "--infer-invariants" in args
    want_shapes = "--shapes" in args
    want_side = "--sidechannel" in args
    want_ct = "--consttime" in args
    args = [a for a in args if not a.startswith("--")]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    path = args[0]
    try:
        program = load_program(path)
        checker = check(program)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1

    print("hlprove — Halis proof report for %s" % path)
    print("")
    lines, totals = proof_report(program, checker)
    if not lines:
        print("  No contracted functions (add a `requires` clause to try "
              "the prover).")
    for key, d, facts, req in lines:
        print("  %s:" % key)
        print("    requires: %s" % (expr_to_text(req) if req else "(none)"))
        if facts:
            for var in sorted(facts):
                print("    seeded fact: %s in %s" % (var, facts[var]))
        print("    PROVEN SAFE: %d overflow, %d division, %d bounds "
              "checks elidable under -O fast"
              % (d["ovf"], d["div"], d["bnd"]))
    print("")
    print("  TOTAL: %d overflow + %d division + %d bounds checks proven "
          "dead" % (totals["ovf"], totals["div"], totals["bnd"]))
    print("  (only PROVEN checks are elided; everything unknown keeps "
          "its runtime panic check)")

    if want_smt:
        out_dir = os.path.dirname(os.path.abspath(path)) or "."
        solver_label = "cvc5" if want_cvc5 else "z3"
        print("")
        print("  SMT-LIB2 bridge (%s-ready, QF_LIA; str lengths "
              "abstracted to Int):" % solver_label)
        files = gen_smt(program, out_dir)
        for key, fpath in files:
            print("    %-28s -> %s" % (key, fpath))
        if want_z3:
            print("")
            run_z3(files)
        elif want_cvc5:
            print("")
            run_cvc5(files)

    if want_inv:
        print("")
        suggest_invariants(program)

    if want_infer:
        print("")
        if want_cvc5:
            solver_name = "cvc5"
            decider = make_cvc5_decider()
        else:
            solver_name = "z3"
            decider = make_decider()
        print("  SMT-based loop-invariant inference (Stage 97; entry "
              "facts + transition relation + template candidates, "
              "%s-checked):" % solver_name)
        out_dir = os.path.dirname(os.path.abspath(path)) or "."
        accepted, rejected = infer_invariants(program, want_smt, out_dir,
                                              decider, solver_name)
        print("  INFERENCE TOTAL: %d invariants verified, %d candidates "
              "rejected" % (accepted, rejected))

    if want_side:
        print("")
        print("  Side-channel audit (Stage 101 — the #[secrets(...)] "
              "parameters are the roots):")
        sidechannel_report(program, checker)

    ct_violated = 0
    if want_ct:
        print("")
        print("  Constant-time verification (Stage 102 — the #[ct] "
              "claims are proven, not trusted):")
        ct_violated = consttime_report(program, checker)

    if want_shapes:
        print("")
        if want_cvc5:
            solver_name = "cvc5"
            decider = make_cvc5_decider()
        else:
            solver_name = "z3"
            decider = make_decider()
        if decider is None:
            print("  (Stage 100 — no %s backend: loop shapes are "
                  "reported as candidates, never proofs)"
                  % solver_name)
        else:
            print("  (Stage 100 — cursor-loop obligations decided by %s)"
                  % solver_name)
        out_dir = os.path.dirname(os.path.abspath(path)) or "."
        shapes_report(program, want_smt, out_dir, decider, solver_name)
    # Stage 102: a violated claim is a failed verification — the run
    # exits 1 after the full report is on the screen (the analysis
    # modes stay exit-0 either way; that difference is the point).
    return 1 if ct_violated else 0


if __name__ == "__main__":
    sys.exit(main())
