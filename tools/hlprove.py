#!/usr/bin/env python3
"""hlprove — the Stage 17 proof assistant for Halis (HLS).

Usage:
  python3 tools/hlprove.py <file.hls> [--fast] [--smt] [--z3] [--cvc5]
                            [--suggest-invariants] [--infer-invariants]

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
