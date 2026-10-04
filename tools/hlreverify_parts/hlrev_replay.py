"""hls-reverify — the replay law: reconcile the evidence with the
replayed verdicts, and assemble the report.

Stage 108. The law, fail-closed:

  A memory-safety CLAIM (a baseline-proven check site) is DISCHARGED by
  the transformed program when
    - the site survives and the independent replay re-proves it, or
    - the site was eliminated (folded to a constant / dead-code
      eliminated — no runtime operation remains at that program point,
      the claim is vacuous), or
    - the site was never lowered (it sat after a return/break/continue
      — the interpreter never executed it either).
  A claim is VIOLATED — LOST-PROOF, the verdict fails — when the site
  survives in its own function and the replay cannot re-prove it.

  Counted conservatively (the assignment problem: several sites can
  share one source line):

      lost = max(0, P - R - eliminated)
        P  proven baseline claims at (fn, kind, line)
        R  replay-proven survivors at the same key
        eliminated = (IR0 - IR1') survivors lost — IR1' is the IR1
                     count minus the INLINE-DERIVED copies (callee
                     bodies copied into callers carry the callee's
                     lines; they are reconciled under the callee's
                     own function, where the contract lives)

  The symmetric classes are informational:
      new-proof   — a survivor proving beyond the proven claims (the
                    optimiser created knowledge the seed prover lacked:
                    inlining + folding); the checks are all still in
                    the IR, nothing elides without a codegen decision.
      replayed-checked — a checked claim's site survives checked.

  Any `safe_overflow` annotation on the transformed IR that neither an
  interval proof nor a pinned algebraic identity certifies is a FORGED
  ANNOTATION (fail). Any dangling operand, dangling label, unreachable
  block or unknown-label branch is BROKEN-IR (fail). An IR0 site with
  no baseline counterpart and no generated-site explanation means the
  lowering INVENTED a check site — DUPLICATED-SITE (fail).
"""
from __future__ import annotations

from . import hlrev_common as com
from . import hlrev_collect as coll
from . import hlrev_ircheck as ircheck
from .hlrev_common import canonical_json  # re-export convenience


def _multiset(rows):
    """{(kind, line): count} over (kind, line, ...) rows."""
    out = {}
    for row in rows:
        key = (row[0], row[1])
        out[key] = out.get(key, 0) + 1
    return out


def _proven_count(sites):
    """{key: proven_count} and {key: total} over (kind, line, proven)."""
    prov = {}
    total = {}
    for kind, line, proven in sites:
        key = (kind, line)
        total[key] = total.get(key, 0) + 1
        if proven:
            prov[key] = prov.get(key, 0) + 1
    return prov, total


def reconcile(entry, baseline, mod0, mod1, analyses, lto):
    """The replay law over all functions. Returns the report dict.

    entry     — the entry path (report metadata)
    baseline  — {fn: [(kind, line, proven)]} from the AST
    mod0      — HLIR pre-optimisation
    mod1      — HLIR post-optimisation (fast=True, optional lto)
    analyses  — {fn: ReplayResult} from the IR replay over mod1
    lto       — the --lto flag (report metadata)
    """
    report = com.new_report(entry, lto)
    counts = {}
    failures = set()

    ir0 = coll.harvest_ir_sites(mod0)
    ir1 = coll.harvest_ir_sites(mod1)

    # ---- global (kind, line) shrink map — attributes inline-derived
    # copies: a callee's site lines appearing in a caller beyond the
    # caller's own IR0 multiset (the inliner pastes callee bodies at
    # the call site, keeping the callee's line numbers) ----
    shrink = {}
    for fname in set(ir0.keys()) | set(ir1.keys()):
        m0 = _multiset(ir0.get(fname, []))
        m1 = _multiset(ir1.get(fname, []))
        for key, c1 in m1.items():
            c0 = m0.get(key, 0)
            if c1 < c0:
                shrink[key] = shrink.get(key, 0) + c0 - c1

    fn_names = sorted(set(list(baseline.keys()) + list(ir0.keys())
                          + list(ir1.keys()) + list(analyses.keys())))
    for fname in fn_names:
        b_sites = baseline.get(fname, [])
        b_prov, b_total = _proven_count(b_sites)
        m0 = _multiset(ir0.get(fname, []))
        m1 = _multiset(ir1.get(fname, []))
        ana = analyses.get(fname)
        verdicts = {}
        if ana is not None:
            for kind, line, dest, proven, cert, detail in ana.verdicts:
                verdicts.setdefault((kind, line), []).append(proven)

        fn_report = {}
        keys = sorted(set(list(b_total.keys()) + list(m0.keys())
                          + list(m1.keys())),
                      key=lambda k: (k[0], k[1]))
        for key in keys:
            kind, line = key
            adv = kind == com.ADVISORY_KIND
            P = b_prov.get(key, 0)
            C = b_total.get(key, 0) - P
            n0 = m0.get(key, 0)
            n1 = m1.get(key, 0)
            # inline-derived copies live in THIS function but were born
            # in a callee that shrank at the same (kind, line)
            inline_derived = 0
            if n1 > n0 and not adv:
                extra = n1 - n0
                avail = shrink.get(key, 0)
                inline_derived = min(extra, avail)
            n1_self = n1 - inline_derived
            eliminated = max(0, n0 - n1_self)
            rv = verdicts.get(key, [])
            R = sum(1 for p in rv[:n1_self] if p)
            F = len(rv[:n1_self]) - R

            if adv or b_total.get(key, 0) == 0:
                # no baseline claim: advisory sites (always checked in
                # every backend), non-contracted functions, inlined
                # copies — the replay still measured them, nothing to
                # discharge
                cls = com.C_NO_CLAIM
                ok = True
            elif n0 == 0:
                # never lowered: unreachable code (after return/break) —
                # the interpreter never executed it either
                cls = com.C_UNREACHABLE
                ok = True
            elif n0 > b_total.get(key, 0):
                # the lowering holds MORE sites than the AST had
                cls = com.C_LOST_PROOF
                ok = False
                failures.add(com.F_DUPLICATED_SITE)
            else:
                # THE LAW: proven claims discharge on re-proof, on
                # elimination, or fail
                lost = max(0, P - R - eliminated)
                if lost > 0:
                    cls = com.C_LOST_PROOF
                    ok = False
                    failures.add(com.F_LOST_PROOF)
                elif n1_self == 0:
                    cls = com.C_ELIMINATED
                    ok = True
                elif R > P:
                    cls = com.C_NEW_PROOF
                    ok = True
                elif R == P and P > 0:
                    cls = com.C_REPLAYED
                    ok = True
                else:
                    # every unproven survivor is covered by a checked
                    # baseline claim — the checks are all still in
                    cls = com.C_REPLAYED_CHECKED
                    ok = True
            counts[cls] = counts.get(cls, 0) + 1
            fn_report["%s@%d" % (kind, line)] = {
                "kind": kind, "line": line,
                "baseline": {"proven": P, "checked": C},
                "ir0": n0, "ir1": n1, "inline_derived": inline_derived,
                "eliminated": eliminated,
                "replay": {"proven": R, "failed": F},
                "class": cls, "ok": ok,
            }
        if b_sites or m0 or m1 or (ana and (ana.verdicts or ana.integrity)):
            report["functions"][fname] = fn_report

    # ---- the annotation audit + integrity, per function ----
    for fname in fn_names:
        ana = analyses.get(fname)
        if ana is None:
            continue
        for line, dest, certified, cert in ana.annotations:
            if not certified:
                failures.add(com.F_FORGED_ANNOTATION)
                report["findings"].append({
                    "class": com.F_FORGED_ANNOTATION,
                    "text": "function %s: safe_overflow on %s at line %d "
                            "carries no certificate (no interval proof, "
                            "no pinned identity) — forged elision"
                            % (fname, dest, line),
                })
            counts["annotations"] = counts.get("annotations", 0) + 1
        for iclass, text in ana.integrity:
            if iclass == "unreachable-block-note":
                # inherited from the lowering, executes nothing — a
                # named note, never a failure
                counts["unreachable-notes"] = \
                    counts.get("unreachable-notes", 0) + 1
                report["findings"].append({
                    "class": "note",
                    "text": text,
                })
                continue
            failures.add(com.F_BROKEN_IR)
            report["findings"].append({
                "class": com.F_BROKEN_IR,
                "text": "%s: %s" % (iclass, text),
            })

    # ---- the LOST-PROOF findings, named per site ----
    for fname, fn_report in report["functions"].items():
        for key, row in fn_report.items():
            if row["class"] == com.C_LOST_PROOF:
                report["findings"].append({
                    "class": com.F_LOST_PROOF,
                    "text": "function %s: %s at line %d — %d proven "
                            "claim(s) the transformed program cannot "
                            "re-prove (survivors %d, replayed %d, "
                            "eliminated %d)"
                            % (fname, row["kind"], row["line"],
                               row["baseline"]["proven"], row["ir1"],
                               row["replay"]["proven"], row["eliminated"]),
                })
            elif row["class"] == com.F_DUPLICATED_SITE:
                report["findings"].append({
                    "class": com.F_DUPLICATED_SITE,
                    "text": "function %s: %s at line %d — the IR holds "
                            "more sites than the lowering can account for"
                            % (fname, row["kind"], row["line"]),
                })

    # ---- headline numbers ----
    report["baseline"]["sites"] = sum(
        sum(1 for _ in sites) for sites in baseline.values())
    report["baseline"]["proven"] = sum(
        sum(1 for s in sites if s[2]) for sites in baseline.values())
    report["baseline"]["checked"] = report["baseline"]["sites"] - \
        report["baseline"]["proven"]
    n_replay_sites = 0
    n_replay_proven = 0
    n_blocks = 0
    for fname in fn_names:
        ana = analyses.get(fname)
        if ana is None:
            continue
        n_blocks += ana.blocks
        for kind, line, dest, proven, cert, detail in ana.verdicts:
            if kind == com.ADVISORY_KIND:
                continue
            n_replay_sites += 1
            if proven:
                n_replay_proven += 1
    report["replay"]["sites"] = n_replay_sites
    report["replay"]["proven"] = n_replay_proven
    report["replay"]["functions"] = len(analyses)
    report["replay"]["blocks"] = n_blocks

    report["counts"] = dict(sorted(counts.items()))
    report["failures"] = sorted(failures)
    report["verdict"] = "REPLAY-FAILED" if failures else "REPLAY-OK"
    return report


def format_text(report) -> str:
    """The human report — the same voice the other Stage tools speak."""
    out = []
    out.append("hls-reverify %s — memory-safety proof replay" % com.VERSION)
    out.append("  entry: %s%s" % (report["entry"],
                                  "  (--lto)" if report["lto"] else ""))
    out.append("  baseline: %d check site(s) — %d proven (elidable), "
               "%d runtime-checked"
               % (report["baseline"]["sites"],
                  report["baseline"]["proven"],
                  report["baseline"]["checked"]))
    out.append("  replay: %d site(s) re-derived over %d function(s), "
               "%d block(s) — %d provable after the optimiser"
               % (report["replay"]["sites"], report["replay"]["functions"],
                  report["replay"]["blocks"], report["replay"]["proven"]))
    if report["counts"]:
        out.append("  classes: %s" % ", ".join(
            "%s=%d" % (k, v) for k, v in report["counts"].items()))
    for f in report["findings"]:
        out.append("  FINDING [%s] %s" % (f["class"], f["text"]))
    out.append("  verdict: %s" % report["verdict"])
    return "\n".join(out)
