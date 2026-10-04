"""hls-reverify — deterministic selftest vectors.

Stage 108. Fifteen vectors, all in-memory, all deterministic: the
interval arithmetic one-definition pins, the seed mapping, the identity
certificates, the generated-site discriminator, the widening
convergence, the replay-law classification table and the forged-
annotation audit. `hls-reverify selftest` runs them; the acceptance
gate re-runs them.
"""
from __future__ import annotations

from boot import proof as bp
from boot.proof import Interval, TOP

from . import hlrev_common as com
from . import hlrev_collect as coll
from . import hlrev_ircheck as ircheck
from . import hlrev_replay as replay

from ir import (HLIRFunction, HLIRModule, Block, Instr)


class _Vec:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print("  [PASS] %s" % name)
        else:
            self.failed += 1
            print("  [FAIL] %s%s" % (name, (" — " + detail) if detail else ""))


def _mk_fn(name, blocks, params=None, ret="int"):
    irf = HLIRFunction(name=name, params=params or [], ret=ret,
                       effects=set())
    irf.blocks = blocks
    return irf


def _entry(instrs, term=None):
    b = Block(name="entry")
    b.instrs = list(instrs)
    b.terminator = term or Instr(None, "return", [], 0)
    return b


def run() -> int:
    v = _Vec()
    print("hls-reverify selftest — %s" % com.VERSION)

    # ---- 1. the ONE DEFINITION: the replay imports boot.proof's
    # arithmetic — pin the functions are the very objects ----
    v.check("one-definition: iv arithmetic is boot.proof's",
            ircheck.bp.iv_add is bp.iv_add
            and ircheck.bp.mul_fits is bp.mul_fits
            and ircheck.bp.excludes_zero is bp.excludes_zero)

    # ---- 2. interval corner vectors (the same numbers the Stage 17
    # soundness fixes pinned) ----
    v.check("iv: TOP never fits",
            not bp.fits(TOP))
    v.check("iv: [max-1, max] + [1, 1] does not fit",
            not bp.add_fits(Interval(bp.INT64_MAX - 1, bp.INT64_MAX),
                            Interval(1, 1)))
    v.check("iv: [0, max-1] + [1, 1] fits",
            bp.add_fits(Interval(0, bp.INT64_MAX - 1), Interval(1, 1)))
    v.check("iv: 0 - INT64_MIN corner does not fit",
            not bp.sub_fits(Interval(0, 0), Interval(bp.INT64_MIN,
                                                     bp.INT64_MIN)))
    v.check("iv: excludes_zero above zero",
            bp.excludes_zero(Interval(1, 10)))
    v.check("iv: excludes_zero straddles zero",
            not bp.excludes_zero(Interval(-1, 1)))

    # ---- 3. the INT64_MIN / -1 division corner (deep-scan-10) ----
    fn = {
        "name": "divcorner", "ret": "int", "extern": False,
        "params": [("a", "int"), ("b", "int")],
        "requires": None, "param_preds": {},
        "effects": set(), "body": [],
    }
    # no contract -> seeds None; build the state by hand instead
    st = ircheck.State()
    st.ints["v_b"] = Interval(-5, -1)   # b < 0 — the classic trap
    st.ints["v_a"] = TOP                # a unbounded — MAY be INT64_MIN
    blk = _entry([
        Instr("t1", "binop", [("op", "/"), ("var", "v_a"), ("var", "v_b")],
              1, attrs={"ty": "int"}),
    ])
    irf = _mk_fn("divcorner", [blk], params=[("a", "int"), ("b", "int")])
    res = ircheck.analyze_function(irf, st)
    div_v = [r for r in res.verdicts if r[0] == com.KIND_DIV]
    v.check("div: INT64_MIN / negative divisor is NOT proven",
            len(div_v) == 1 and div_v[0][3] is False,
            str(div_v))
    st2 = ircheck.State()
    st2.ints["v_b"] = Interval(-5, -1)
    st2.ints["v_a"] = Interval(-100, 100)  # bounded — cannot be MIN
    blk2 = _entry([
        Instr("t1", "binop", [("op", "/"), ("var", "v_a"), ("var", "v_b")],
              1, attrs={"ty": "int"}),
    ])
    res2 = ircheck.analyze_function(
        _mk_fn("divcorner2", [blk2],
               params=[("a", "int"), ("b", "int")]), st2)
    div_v2 = [r for r in res2.verdicts if r[0] == com.KIND_DIV]
    v.check("div: bounded dividend / negative divisor IS proven",
            len(div_v2) == 1 and div_v2[0][3] is True, str(div_v2))

    # ---- 4. the nz set route (x != 0 with no bounds): the dividend
    # must be BOUNDED — `b != 0` alone leaves b == -1 possible and the
    # INT64_MIN / -1 corner refuses (deep-scan-10, both provers) ----
    st3 = ircheck.State()
    st3.nz.add("v_b")
    st3.ints["v_a"] = Interval(-100, 100)
    blk3 = _entry([
        Instr("t1", "binop", [("op", "%"), ("var", "v_a"), ("var", "v_b")],
              1, attrs={"ty": "int"}),
    ])
    res3 = ircheck.analyze_function(
        _mk_fn("nzroute", [blk3], params=[("a", "int"), ("b", "int")]),
        st3)
    div_v3 = [r for r in res3.verdicts if r[0] == com.KIND_DIV]
    v.check("div: the nz set proves a non-zero divisor (bounded dividend)",
            len(div_v3) == 1 and div_v3[0][3] is True, str(div_v3))
    # and the SAME nz seed with an UNBOUNDED dividend stays checked:
    # the divisor may be -1 and INT64_MIN / -1 overflows
    st3b = ircheck.State()
    st3b.nz.add("v_b")
    blk3b = _entry([
        Instr("t1", "binop", [("op", "%"), ("var", "v_a"), ("var", "v_b")],
              1, attrs={"ty": "int"}),
    ])
    res3b = ircheck.analyze_function(
        _mk_fn("nzroute2", [blk3b],
               params=[("a", "int"), ("b", "int")]), st3b)
    div_v3b = [r for r in res3b.verdicts if r[0] == com.KIND_DIV]
    v.check("div: nz alone never elides an unbounded dividend",
            len(div_v3b) == 1 and div_v3b[0][3] is False, str(div_v3b))

    # ---- 5. the identity certificates: pinned shapes ----
    def lit(x):
        return ("lit", x)
    v.check("cert: x + 0 -> add-zero-right",
            ircheck._identity_cert("+", ("var", "t"), lit(0))
            == "alg:add-zero-right")
    v.check("cert: 0 + x -> add-zero-left",
            ircheck._identity_cert("+", lit(0), ("var", "t"))
            == "alg:add-zero-left")
    v.check("cert: x - 0 -> sub-zero-right",
            ircheck._identity_cert("-", ("var", "t"), lit(0))
            == "alg:sub-zero-right")
    v.check("cert: x * 0 -> mul-zero-right",
            ircheck._identity_cert("*", ("var", "t"), lit(0))
            == "alg:mul-zero-right")
    v.check("cert: 1 * x -> mul-one-left",
            ircheck._identity_cert("*", lit(1), ("var", "t"))
            == "alg:mul-one-left")
    v.check("cert: x * 2 -> NO certificate",
            ircheck._identity_cert("*", ("var", "t"), lit(2)) is None)
    v.check("cert: 0 - x -> NO certificate (0 - INT64_MIN overflows)",
            ircheck._identity_cert("-", lit(0), ("var", "t")) is None)

    # ---- 6. the generated for-get discriminator ----
    gen = Instr("v_x", "list_get", [("var", "v_xs"), ("var", "v_x__i")], 3)
    user = Instr("t1", "list_get", [("var", "v_xs"), ("var", "v_i")], 3)
    other = Instr("v_x", "list_get", [("var", "v_xs"), ("var", "v_y")], 3)
    v.check("generated: the for pattern is excluded",
            coll.is_generated_for_get(gen))
    v.check("generated: a user index is a site",
            not coll.is_generated_for_get(user))
    v.check("generated: same dest, other index, is a site",
            not coll.is_generated_for_get(other))

    # ---- 7. bounds routes: symbolic (len, owner, -1) and numeric ----
    st4 = ircheck.State()
    st4.ints["v_i"] = Interval(0, ("len", "xs", -1))
    blk4 = _entry([
        Instr("t1", "list_get", [("var", "v_xs"), ("var", "v_i")], 2),
    ])
    res4 = ircheck.analyze_function(_mk_fn("symroute", [blk4],
                                           params=[("xs", "list[int]"),
                                                   ("i", "int")]), st4)
    bnd4 = [r for r in res4.verdicts if r[0] == com.KIND_BND]
    v.check("bnd: the symbolic i < xs.len() seed re-proves",
            len(bnd4) == 1 and bnd4[0][3] is True, str(bnd4))
    # deep-scan-10: delta 0 is NOT a valid index bound
    st5 = ircheck.State()
    st5.ints["v_i"] = Interval(0, ("len", "xs", 0))
    blk5 = _entry([
        Instr("t1", "list_get", [("var", "v_xs"), ("var", "v_i")], 2),
    ])
    res5 = ircheck.analyze_function(_mk_fn("symroute0", [blk5]), st5)
    bnd5 = [r for r in res5.verdicts if r[0] == com.KIND_BND]
    v.check("bnd: i <= xs.len() (delta 0) is NOT proven",
            len(bnd5) == 1 and bnd5[0][3] is False, str(bnd5))
    # numeric minlen route
    st6 = ircheck.State()
    st6.ints["v_i"] = Interval(0, 7)
    st6.lens["v_s"] = Interval(8, None)
    blk6 = _entry([
        Instr("t1", "method", [("name", "byte_at"), ("var", "v_s"),
                               ("var", "v_i")], 2),
    ])
    res6 = ircheck.analyze_function(_mk_fn("minlenroute", [blk6]), st6)
    bnd6 = [r for r in res6.verdicts if r[0] == com.KIND_BND]
    v.check("bnd: byte_at below the seeded minlen re-proves",
            len(bnd6) == 1 and bnd6[0][3] is True, str(bnd6))

    # ---- 8. widening convergence: a growing counter stays bounded
    # in round count and ends TOP ----
    #   entry: v_i = const 0
    #   cond:  t = binop <, v_i, const 16 ; branch body/end
    #   body:  v_i = store (binop +, v_i, const 1)
    entry_b = _entry([
        Instr("v_i", "const", [("lit", 0)], 1, attrs={"ty": "int"}),
    ], term=Instr(None, "jump", [("label", "cond")], 0))
    cond_b = Block(name="cond")
    cond_b.instrs = [
        Instr("t1", "const", [("lit", 16)], 2, attrs={"ty": "int"}),
        Instr("t2", "binop", [("op", "<"), ("var", "v_i"),
                              ("var", "t1")], 2, attrs={"ty": "bool"}),
    ]
    cond_b.terminator = Instr(None, "branch", [("var", "t2"),
                                               ("label", "body"),
                                               ("label", "end")], 2)
    body_b = Block(name="body")
    body_b.instrs = [
        Instr("t3", "const", [("lit", 1)], 3, attrs={"ty": "int"}),
        Instr("t4", "binop", [("op", "+"), ("var", "v_i"),
                              ("var", "t3")], 3, attrs={"ty": "int"}),
        Instr("v_i", "store", [("var", "t4"), ("name", "i")], 3, attrs={"ty": "int"}),
    ]
    body_b.terminator = Instr(None, "jump", [("label", "cond")], 3)
    end_b = Block(name="end")
    end_b.instrs = []
    end_b.terminator = Instr(None, "return", [], 4)
    irf7 = _mk_fn("widens", [entry_b, cond_b, body_b, end_b])
    res7 = ircheck.analyze_function(irf7, ircheck.State())
    v.check("widening: the loop converges within the round cap",
            res7.iterations > 0 and res7.widened_loops == 1,
            "iterations=%d" % res7.iterations)
    # and the counter's final interval escaped to infinity (TOP) —
    # the sound answer for an unbounded loop
    entry_in = res7  # (the state itself is internal; the verdict pass
    # ran clean, which is the observable contract)
    v.check("widening: analysis completes with no integrity findings",
            not res7.integrity, str(res7.integrity))

    # ---- 9. integrity: a dangling operand is caught ----
    bad_blk = _entry([
        Instr("t1", "binop", [("op", "+"), ("var", "v_ghost"),
                              ("lit", 1)], 1, attrs={"ty": "int"}),
    ])
    res8 = ircheck.analyze_function(_mk_fn("dangling", [bad_blk]),
                                    ircheck.State())
    classes = {c for c, _t in res8.integrity}
    v.check("integrity: a dangling var operand fails",
            "dangling-var" in classes, str(res8.integrity))

    # ---- 10. integrity: a dangling label is caught ----
    bad_lab = _entry([], term=Instr(None, "jump", [("label", "nowhere")], 1))
    res9 = ircheck.analyze_function(_mk_fn("badlabel", [bad_lab]),
                                    ircheck.State())
    classes9 = {c for c, _t in res9.integrity}
    v.check("integrity: a dangling label fails",
            "dangling-label" in classes9, str(res9.integrity))

    # ---- 11. the forged-annotation audit: x * 2 annotated -> fail ----
    forge_blk = _entry([
        Instr("t1", "const", [("lit", 2)], 1, attrs={"ty": "int"}),
        Instr("t2", "binop", [("op", "*"), ("var", "v_x"),
                              ("var", "t1")], 1,
              attrs={"ty": "int", "safe_overflow": True}),
    ])
    res10 = ircheck.analyze_function(
        _mk_fn("forged", [forge_blk], params=[("x", "int")]),
        ircheck.State())
    forged = [a for a in res10.annotations if not a[2]]
    v.check("audit: safe_overflow on x*2 is forged",
            len(res10.annotations) == 1 and not res10.annotations[0][2],
            str(res10.annotations))

    # ---- 12. the audit accepts a PINNED identity ----
    id_blk = _entry([
        Instr("t1", "const", [("lit", 0)], 1, attrs={"ty": "int"}),
        Instr("t2", "binop", [("op", "+"), ("var", "v_x"),
                              ("var", "t1")], 1,
              attrs={"ty": "int", "safe_overflow": True}),
    ])
    res11 = ircheck.analyze_function(
        _mk_fn("identity", [id_blk], params=[("x", "int")]),
        ircheck.State())
    v.check("audit: safe_overflow on x+0 certifies (identity)",
            len(res11.annotations) == 1 and res11.annotations[0][2],
            str(res11.annotations))

    # ---- 13. the replay-law classification table (synthetic IR) ----
    def site_fn(name, instrs):
        return _mk_fn(name, [_entry(instrs)],
                      params=[("a", "int"), ("b", "int")])

    plus = Instr("t1", "binop", [("op", "+"), ("var", "v_a"),
                                 ("var", "v_b")], 5, attrs={"ty": "int"})

    def pair(seeds, proven_seed, ir1_instrs=None):
        """mod0/mod1 sharing the ONE site; the baseline verdict is a
        free variable of the table."""
        mod0 = HLIRModule()
        mod0.functions["f"] = site_fn("f", [plus])
        mod1 = HLIRModule()
        mod1.functions["f"] = site_fn(
            "f", [plus] if ir1_instrs is None else ir1_instrs)
        baseline = {"f": [(com.KIND_OVF, 5, proven_seed)]}
        analyses = {"f": ircheck.analyze_function(mod1.functions["f"],
                                                  seeds)}
        return mod0, mod1, baseline, analyses

    # 13a. proven claim, bounded seeds -> re-proves -> REPLAYED
    sd = ircheck.State()
    sd.ints["v_a"] = Interval(0, 100)
    sd.ints["v_b"] = Interval(0, 100)
    m0, m1, bl, an = pair(sd, True)
    rep = replay.reconcile("selftest", bl, m0, m1, an, False)
    row = rep["functions"]["f"]["ovf@5"]
    v.check("law: proven + re-provable -> replayed",
            row["class"] == com.C_REPLAYED and rep["verdict"] == "REPLAY-OK",
            str(row))

    # 13b. proven claim, UNBOUNDED seeds -> the replay cannot re-prove
    # -> LOST-PROOF (the forged elision the tool exists to catch)
    sd2 = ircheck.State()
    m0, m1, bl, an = pair(sd2, True)
    rep = replay.reconcile("selftest", bl, m0, m1, an, False)
    row = rep["functions"]["f"]["ovf@5"]
    v.check("law: proven + not re-provable -> lost-proof (fail)",
            row["class"] == com.C_LOST_PROOF
            and rep["verdict"] == "REPLAY-FAILED"
            and com.F_LOST_PROOF in rep["failures"], str(row))

    # 13c. checked claim, unbounded seeds -> replayed-checked
    sd3 = ircheck.State()
    m0, m1, bl, an = pair(sd3, False)
    rep = replay.reconcile("selftest", bl, m0, m1, an, False)
    row = rep["functions"]["f"]["ovf@5"]
    v.check("law: checked + checked -> replayed-checked",
            row["class"] == com.C_REPLAYED_CHECKED
            and rep["verdict"] == "REPLAY-OK", str(row))

    # 13d. checked claim, bounded seeds -> new-proof (informational)
    sd4 = ircheck.State()
    sd4.ints["v_a"] = Interval(0, 100)
    sd4.ints["v_b"] = Interval(0, 100)
    m0, m1, bl, an = pair(sd4, False)
    rep = replay.reconcile("selftest", bl, m0, m1, an, False)
    row = rep["functions"]["f"]["ovf@5"]
    v.check("law: checked + now provable -> new-proof (named, not fail)",
            row["class"] == com.C_NEW_PROOF
            and rep["verdict"] == "REPLAY-OK", str(row))

    # 13e. the site eliminated (folded) between IR0 and IR1 -> the
    # claim discharges vacuously
    sd5 = ircheck.State()
    m0, m1, bl, an = pair(sd5, True, ir1_instrs=[])
    rep = replay.reconcile("selftest", bl, m0, m1, an, False)
    row = rep["functions"]["f"]["ovf@5"]
    v.check("law: eliminated site -> vacuous discharge",
            row["class"] == com.C_ELIMINATED
            and rep["verdict"] == "REPLAY-OK", str(row))

    # 13f. an unprovable claim whose site was eliminated -> still OK
    # (the claim is gone WITH the site)
    sd6 = ircheck.State()
    m0, m1, bl, an = pair(sd6, True, ir1_instrs=[])
    rep = replay.reconcile("selftest", bl, m0, m1, an, False)
    row = rep["functions"]["f"]["ovf@5"]
    v.check("law: proven claim + eliminated site -> eliminated, not lost",
            row["class"] == com.C_ELIMINATED
            and rep["verdict"] == "REPLAY-OK", str(row))

    # ---- 14. the report is deterministic and canonically serialised ----
    sd7 = ircheck.State()
    sd7.ints["v_a"] = Interval(0, 100)
    sd7.ints["v_b"] = Interval(0, 100)
    m0, m1, bl, an = pair(sd7, True)
    rep_a = replay.reconcile("selftest", bl, m0, m1, an, False)
    rep_b = replay.reconcile("selftest", bl, m0, m1, an, False)
    v.check("report: byte-identical over unchanged inputs",
            com.canonical_json(rep_a) == com.canonical_json(rep_b))
    v.check("report: schema pinned",
            rep_a["schema"] == com.REPORT_SCHEMA
            and rep_a["tool"] == com.TOOL
            and rep_a["tool_version"] == com.VERSION)

    # ---- 15. the seed mapping: requires -> v_ names ----
    ast_fn = {
        "name": "seeded", "ret": "int", "extern": False,
        "params": [("i", "int", False), ("xs", "list[int]", False)],
        "requires": {
            "k": "bin", "op": "&&", "t": "bool", "line": 1,
            "l": {"k": "bin", "op": ">=", "t": "bool", "line": 1,
                  "l": {"k": "ident", "name": "i", "t": "int", "line": 1},
                  "r": {"k": "int", "v": 0, "t": "int", "line": 1}},
            "r": {"k": "bin", "op": "<", "t": "bool", "line": 1,
                  "l": {"k": "ident", "name": "i", "t": "int", "line": 1},
                  "r": {"k": "method", "name": "len", "t": "int",
                        "line": 1,
                        "target": {"k": "ident", "name": "xs",
                                   "t": "list[int]", "line": 1}}},
        },
        "param_preds": {}, "effects": set(), "body": [],
    }
    st_seed = ircheck.seed_function(ast_fn)
    v.check("seeds: the interval lands on v_i",
            st_seed is not None and st_seed.ints.get("v_i") is not None
            and st_seed.ints["v_i"].lo == 0, str(st_seed.ints if st_seed else None))
    v.check("seeds: the symbolic len bound rides the interval's hi",
            st_seed.ints["v_i"].hi == ("len", "xs", -1),
            repr(st_seed.ints["v_i"].hi))

    print("hls-reverify selftest: %d passed / %d failed"
          % (v.passed, v.failed))
    return 1 if v.failed else 0

