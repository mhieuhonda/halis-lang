#!/usr/bin/env bash
# Suite suite_02_differential - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

echo "=== 4. Differential test for examples (with data file) ==="
python3 boot/boot.py examples/wordcount.hls examples/data.txt > "$TMP/wc_interp.txt" 2>/dev/null
python3 boot/boot.py src/hlc.hls examples/wordcount.hls "$TMP/wc.c" >/dev/null 2>&1
gcc -O2 -o "$TMP/wc.bin" "$TMP/wc.c" -lm -pthread 2>/dev/null
"$TMP/wc.bin" examples/data.txt > "$TMP/wc_nat.txt" 2>/dev/null
if diff -q "$TMP/wc_interp.txt" "$TMP/wc_nat.txt" >/dev/null 2>&1; then
    ok "wordcount (native vs interpreter)"
else
    bad "wordcount"
fi

echo "=== 4a. Stage 16: NATIVE hlc compiles every ok program ==="
# Deep-scan-9 test-gap fix: section 3 above compiles the ok/ programs
# with the INTERPRETED compiler (Stage-0). The NATIVE binary was only
# exercised on hlc.hls itself + fibonacci — which let a heap-use-after-
# free in the native codegen (double field-release for match/qmark .t)
# hide until feat_clone_deep crashed it. From now on, the native
# compiler must compile (and, where possible, run) EVERY ok program.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
nat_ok=0
nat_bad=0
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    # Deep-scan-28 fix: `#![freestanding]` crates emit `_start` and no
    # `main`, so the hosted `gcc -O2` recipe double-defines `_start`
    # (Scrt1.o) and the whole section reported a false failure since
    # Stage 77 added tests/ok/feat_freestanding_basic.hls. Use the same
    # freestanding link recipe suite_01_boot.sh uses for those files.
    if head -5 "$f" | grep -q '#!\[freestanding\]'; then
        if "$TMP/hlc1" "$f" "$TMP/$name.nat.c" >/dev/null 2>&1 \
                && gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
                       -fno-stack-protector -Wl,--gc-sections \
                       -o "$TMP/$name.nat.bin" "$TMP/$name.nat.c" 2>/dev/null; then
            nat_ok=$((nat_ok+1))
        else
            nat_bad=$((nat_bad+1))
            bad "native hlc compile: $name"
        fi
    elif "$TMP/hlc1" "$f" "$TMP/$name.nat.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.nat.bin" "$TMP/$name.nat.c" -lm -pthread 2>/dev/null; then
        nat_ok=$((nat_ok+1))
    else
        nat_bad=$((nat_bad+1))
        bad "native hlc compile: $name"
    fi
done
if [ $nat_bad -eq 0 ]; then
    ok "native hlc compiles + gcc-builds all $nat_ok ok programs"
fi

echo "=== 4b. Stage 17: contracts + proof (fast-mode differential) ==="
# The proof elision must be semantics-preserving: the -O fast build
# (checks elided where PROVEN) must produce byte-identical output to
# the interpreter for the contracted test programs.
# Deep-scan-10 (Stage-17 perfection): the soundness regressions are
# also compiled -O fast — their checks must NOT be elided, so both
# implementations must panic identically (101) byte for byte.
for f in tests/ok/feat_contract_*.hls tests/ok/feat_proof_elide.hls \
         tests/ok/feat_proof_sound_*.hls; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .hls)
    if [ ! -x "$TMP/hlc1" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
    fi
    interp_out=$(timeout 60 python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_rc=$?
    if "$TMP/hlc1" --fast "$f" "$TMP/$name.fast.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.fast.bin" "$TMP/$name.fast.c" -lm -pthread 2>/dev/null; then
        fast_out=$(timeout 60 "$TMP/$name.fast.bin" </dev/null 2>/dev/null); fast_rc=$?
        if [ "$interp_out" == "$fast_out" ] && [ "$interp_rc" == "$fast_rc" ]; then
            ok "$name (-O fast output identical to interpreter)"
        else
            bad "$name (-O fast diverges: interp=$interp_rc fast=$fast_rc)"
        fi
    else
        bad "$name (fast compile failed)"
    fi
done
# hlprove must run cleanly on the acceptance example and report elisions.
if python3 tools/hlprove.py examples/hmac_proven.hls >/dev/null 2>&1; then
    ok "hlprove runs on the HMAC acceptance example"
else
    bad "hlprove failed on the HMAC acceptance example"
fi
# Stage 97: SMT-based loop-invariant inference. The engine must run
# clean on the demo, claim the strengthened accumulator, and REFUSE the
# non-inductive entry bound (the gate pins the full battery; this pins
# the pipeline is wired).
inv_out=$(python3 tools/hlprove.py examples/invariant_demo.hls --infer-invariants 2>/dev/null); inv_rc=$?
if [ "$inv_rc" -eq 0 ] && echo "$inv_out" | grep -q "      s >= 0   (entry-bound)" \
   && echo "$inv_out" | grep -q "rejected: i >= 0 (preservation obligation not discharged)" \
   && echo "$inv_out" | grep -q "INFERENCE TOTAL:"; then
    ok "hlprove --infer-invariants discovers and refuses on the demo"
else
    bad "hlprove --infer-invariants wrong on the demo (rc=$inv_rc)"
fi

# Stage 99: the CVC5 SMT backend — the same bridge, a second decider.
# The run must be clean with or without cvc5 installed (the report
# honestly says when neither binary nor module exists); with a cvc5
# binary on PATH the per-fn verdict lines must appear.
cvc5_out=$(python3 tools/hlprove.py examples/hmac_proven.hls --cvc5 2>/dev/null); cvc5_rc=$?
if [ "$cvc5_rc" -eq 0 ] && echo "$cvc5_out" | grep -q "cvc5"; then
    ok "hlprove --cvc5 runs the backend on the HMAC example"
else
    bad "hlprove --cvc5 wrong on the HMAC example (rc=$cvc5_rc)"
fi
if command -v cvc5 >/dev/null 2>&1; then
    if echo "$cvc5_out" | grep -q "cvc5: vacuity: sat"; then
        ok "hlprove --cvc5 reports the HMAC verdicts (binary on PATH)"
    else
        bad "hlprove --cvc5 verdict lines missing (cvc5 binary on PATH)"
    fi
fi

# Stage 100: the separation-logic fragment — heap shapes. The run must
# be clean with or without a solver installed (the report is honest
# about missing backends); with a decider on PATH the proven triple,
# the frame refusal and the separation verdicts must appear on the
# heap demo.
shape_out=$(python3 tools/hlprove.py examples/heap_demo.hls --shapes 2>/dev/null); shape_rc=$?
if [ "$shape_rc" -eq 0 ] && echo "$shape_out" | grep -q "SHAPES TOTAL:"; then
    ok "hlprove --shapes runs the fragment on the heap demo"
else
    bad "hlprove --shapes wrong on the heap demo (rc=$shape_rc)"
fi
if python3 -c "import z3" 2>/dev/null || command -v z3 >/dev/null 2>&1; then
    if echo "$shape_out" | grep -q "shape PROVEN (z3): lseg(xs, 0, i) \* cell(xs, i)" \
       && echo "$shape_out" | grep -q "frame REFUSED" \
       && echo "$shape_out" | grep -q "own(a) \* own(b) REFUSED" \
       && echo "$shape_out" | grep -q "own(a) \* own(buf) PROVEN"; then
        ok "hlprove --shapes proves and refuses on the demo (decider on PATH)"
    else
        bad "hlprove --shapes verdict lines missing (decider on PATH)"
    fi
fi

# Stage 101: the cryptographic side-channel analysis. The run must be
# clean; on the demo every sink kind (branch, index, loop-bound,
# division), the incoming chain, the policy note and the totals line
# must appear.
sc_out=$(python3 tools/hlprove.py examples/sidechannel_demo.hls --sidechannel 2>/dev/null); sc_rc=$?
if [ "$sc_rc" -eq 0 ] && echo "$sc_out" | grep -q "SIDECHANNEL TOTAL:"; then
    ok "hlprove --sidechannel runs on the side-channel demo"
else
    bad "hlprove --sidechannel wrong on the demo (rc=$sc_rc)"
fi
if echo "$sc_out" | grep -qF 'INDEX — `sbox.get(b)`' \
   && echo "$sc_out" | grep -qF 'BRANCH — `(key.byte_at(i) != probe.byte_at(i))`' \
   && echo "$sc_out" | grep -qF 'LOOP-BOUND — `range(0, rounds)`' \
   && echo "$sc_out" | grep -qF 'DIVISION — `(acc % rounds)`' \
   && echo "$sc_out" | grep -qF 'public by policy' \
   && echo "$sc_out" | grep -qF 'SIDECHANNEL TOTAL: 5 leaks'; then
    ok "hlprove --sidechannel reports every sink kind on the demo"
else
    bad "hlprove --sidechannel findings missing on the demo"
fi


# Stage 110: sandboxed execution (seccomp-bpf). The profile must
# derive the demo's surface honestly (write_file is reachable -> rw),
# --fs ro tightens with the flag named as the origin, the package
# demo's [sandbox] pins survive the gate, and the selftest's pinned
# numbers are green. The kernel half (the confined runs) lives in
# the acceptance gate, which owns the compile.
sb_out=$(python3 tools/hls-sandbox.py profile examples/sandbox_demo.hls 2>/dev/null); sb_rc=$?
if [ "$sb_rc" -eq 0 ] && echo "$sb_out" | grep -q "execution surface (checker, from main): Args, Fs, IO"; then
    ok "hls-sandbox profiles the demo (source mode)"
else
    bad "hls-sandbox wrong on the demo (rc=$sb_rc)"
fi
if echo "$sb_out" | grep -qF 'fs mode: rw (derived — the reachable builtin census contains' \
   && echo "$sb_out" | grep -qF 'reachable builtins: args, println, read_file, write_file'; then
    ok "the derivation is static and conservative (write_file reachable -> rw)"
else
    bad "the derived fs split is missing on the demo"
fi
sb_ro=$(python3 tools/hls-sandbox.py profile examples/sandbox_demo.hls --fs ro 2>/dev/null)
if echo "$sb_ro" | grep -qF 'fs mode: ro (--fs)'; then
    ok "--fs ro tightens below the derivation, origin named"
else
    bad "--fs ro did not tighten"
fi
sb_pkg=$(python3 tools/hls-sandbox.py profile --pkg examples/pkg_sandbox_demo 2>/dev/null); sbp_rc=$?
if [ "$sbp_rc" -eq 0 ] \
   && echo "$sb_pkg" | grep -qF 'execution surface (checker, from main): Clock, Fs, IO' \
   && echo "$sb_pkg" | grep -qF 'fs mode: ro' \
   && echo "$sb_pkg" | grep -qF 'denied: clone, fork, vfork' \
   && echo "$sb_pkg" | grep -qF 'gate: manifest [effects].allowed (drift 0, unauditable 0, violations 0)'; then
    ok "package mode: the gate passes and the [sandbox] pins hold"
else
    bad "package mode wrong on the demo (rc=$sbp_rc)"
fi
sb_st=$(python3 tools/hls-sandbox.py selftest 2>/dev/null); sbs_rc=$?
if [ "$sbs_rc" -eq 0 ] && echo "$sb_st" | grep -q "37 passed / 0 failed"; then
    ok "hls-sandbox selftest green (37 vectors)"
else
    bad "hls-sandbox selftest failed (rc=$sbs_rc)"
fi

# Stage 109: taint-tracking through FFI boundaries. The demo's report
# must census the boundary (1 source), name the entry point with its
# provenance, cut on the sanitizer, list the accepted-risk sink flow,
# and end on the pinned total.
ft_out=$(python3 tools/hlprove.py examples/ffi_taint_demo.hls --taint 2>/dev/null); ft_rc=$?
if [ "$ft_rc" -eq 0 ] && echo "$ft_out" | grep -q "FFI TAINT TOTAL:"; then
    ok "hlprove --taint runs on the FFI-taint demo"
else
    bad "hlprove --taint wrong on the demo (rc=$ft_rc)"
fi
if echo "$ft_out" | grep -qF 'dirname(p: str) -> str  [SOURCE #[taint_source]' \
   && echo "$ft_out" | grep -qF 'boundary census: 1 source(s), 0 sink(s), 0 unmarked' \
   && echo "$ft_out" | grep -qF 'SOURCE — `dirname("...")` (from ffi:dirname)' \
   && echo "$ft_out" | grep -qF 'SANITISED — `sanitize_path(raw)` (from ffi:dirname)' \
   && echo "$ft_out" | grep -qF 'SINK-REACH — `println(("..." + raw_dir))` (from ffi:dirname)' \
   && echo "$ft_out" | grep -qF 'FFI TAINT TOTAL: 1 source sites, 4 unwrap escapes, 1 sink-reaching flows, 1 sanitised cuts'; then
    ok "hlprove --taint reports the census, the provenance and the pinned total"
else
    bad "hlprove --taint findings missing on the demo"
fi

# Stage 102: the constant-time verifier. The demo's claims must all
# verify (exit 0); the policy note and the transitive reach must be
# printed; and a violated claim must exit 1 — the verifier's teeth.
ct_out=$(python3 tools/hlprove.py examples/consttime_demo.hls --consttime 2>/dev/null); ct_rc=$?
if [ "$ct_rc" -eq 0 ] && echo "$ct_out" | grep -q "CT TOTAL: 3 verified, 0 violated of 3 claims"; then
    ok "hlprove --consttime verifies every claim on the demo"
else
    bad "hlprove --consttime wrong on the demo (rc=$ct_rc)"
fi
if echo "$ct_out" | grep -qF 'policy: .len() on a secret value — 1 use(s), public by policy' \
   && echo "$ct_out" | grep -qF 'secret reach: ct_expand, schedule_step (2 fns)'; then
    ok "hlprove --consttime prints the policy note and the reach"
else
    bad "hlprove --consttime report lines missing on the demo"
fi
cat > "$TMP/ct_violated.hls" <<'CTSEOF'
#[secrets(s)]
#[ct]
fn gate(s: int) -> int {
    if s > 10 {
        return 1
    }
    return 0
}

fn main() -> int uses IO {
    println(gate(3).to_str())
    return 0
}
CTSEOF
ctv_out=$(python3 tools/hlprove.py "$TMP/ct_violated.hls" --consttime 2>/dev/null); ctv_rc=$?
if [ "$ctv_rc" -eq 1 ] && echo "$ctv_out" | grep -q "CT TOTAL: 0 verified, 1 violated of 1 claims" \
   && echo "$ctv_out" | grep -q "chain: gate (the claim's own body)"; then
    ok "hlprove --consttime exits 1 on a violated claim"
else
    bad "hlprove --consttime exit code wrong on the violated claim (rc=$ctv_rc)"
fi

# Stage 103: hls-audit, the transitive supply-chain effect report.
# The demo's import tree must classify (toolchain vs workspace),
# attribute Net/Clock/IO to their introduction modules, and exit 0;
# gated with --allow IO,Net the Clock chain must FAIL the run; and
# the package demo must walk INTO the nested manifest and name
# audit_lib_b at depth 2 (the hop hls-pkg audit never takes).
s103_out=$(python3 tools/hls-audit.py examples/audit_demo.hls 2>/dev/null); s103_rc=$?
if [ "$s103_rc" -eq 0 ] \
   && echo "$s103_out" | grep -qF 'modules: 4 (1 toolchain, 0 dependency, 3 workspace)' \
   && echo "$s103_out" | grep -qF 'Clock — introduced by examples/audit_libs/geo.hls [workspace]' \
   && echo "$s103_out" | grep -qF 'IO — introduced by examples/audit_libs/netcheck.hls [workspace]' \
   && echo "$s103_out" | grep -qF 'extern "C" { labs } declared (none)' \
   && echo "$s103_out" | grep -qF 'program effect surface: Clock, IO, Net'; then
    ok "hls-audit classifies and attributes the demo's import tree"
else
    bad "hls-audit report wrong on the demo (rc=$s103_rc)"
fi
s103g_out=$(python3 tools/hls-audit.py examples/audit_demo.hls --allow IO,Net 2>/dev/null); s103g_rc=$?
if [ "$s103g_rc" -eq 1 ] \
   && echo "$s103g_out" | grep -qF 'Clock (introduced by examples/audit_libs/geo.hls)' \
   && echo "$s103g_out" | grep -qF 'verdict: VIOLATED — 1 effect(s) outside the allow list'; then
    ok "hls-audit --allow gates the chain (exit 1 with the chain)"
else
    bad "hls-audit --allow wrong on the demo (rc=$s103g_rc)"
fi
s103p_out=$(python3 tools/hls-audit.py --pkg examples/pkg_audit_demo 2>/dev/null); s103p_rc=$?
if [ "$s103p_rc" -eq 1 ] \
   && echo "$s103p_out" | grep -qF 'packages: 3 (root included) — deepest chain: 2 dep(s)' \
   && echo "$s103p_out" | grep -qF 'Fs — introduced by audit_lib_b (depth 2)' \
   && echo "$s103p_out" | grep -qF 'via: pkg_audit_demo -> audit_lib_a -> audit_lib_b' \
   && echo "$s103p_out" | grep -qF 'policy: allowed = IO (from manifest)' \
   && echo "$s103p_out" | grep -qF 'verdict: VIOLATED — 2 effect(s) outside the allowed set'; then
    ok "hls-audit --pkg walks the nested manifest and fails the run"
else
    bad "hls-audit --pkg wrong on the package demo (rc=$s103p_rc)"
fi

# Stage 104: hls-sbom, the CycloneDX + SPDX bill of materials. The
# source-mode documents list the demo's import tree (one application,
# three file components, hashed) with the graph in dependencies /
# DEPENDS_ON; the package-mode documents list the manifest tree; and
# the documents are content-addressed — two runs over the same tree
# with the same SOURCE_DATE_EPOCH are byte-identical.
s104_cdx=$(SOURCE_DATE_EPOCH=1727800000 python3 tools/hls-sbom.py examples/sbom_demo.hls --format cdx --stdout 2>/dev/null); s104_rc=$?
if [ "$s104_rc" -eq 0 ] \
   && echo "$s104_cdx" | python3 -c 'import json,sys; d=json.load(sys.stdin); comps={c["name"]: c for c in d["components"]}; deps={x["ref"]: x["dependsOn"] for x in d["dependencies"]}; import os,hashlib; sha=lambda p: hashlib.sha256(open(p,"rb").read()).hexdigest(); sys.exit(0 if (d["bomFormat"]=="CycloneDX" and d["specVersion"]=="1.5" and len(comps)==3 and comps["std/str.hls"]["type"]=="file" and comps["std/str.hls"]["version"]=="0.123.0-alpha" and comps["examples/sbom_libs/dep_b.hls"]["hashes"][0]["content"]==sha("examples/sbom_libs/dep_b.hls") and deps["hls:module:examples/sbom_demo.hls"]==["hls:module:examples/sbom_libs/dep_a.hls","hls:module:std/str.hls"]) else 1)';
then
    ok "hls-sbom lists the demo tree in CycloneDX (components, hashes, graph)"
else
    bad "hls-sbom CycloneDX document wrong on the demo (rc=$s104_rc)"
fi
s104_spdx=$(SOURCE_DATE_EPOCH=1727800000 python3 tools/hls-sbom.py examples/sbom_demo.hls --format spdx --stdout 2>/dev/null); s104s_rc=$?
if [ "$s104s_rc" -eq 0 ] \
   && echo "$s104_spdx" | python3 -c 'import json,sys; d=json.load(sys.stdin); pkgs={p["name"]: p for p in d["packages"]}; rels=[r for r in d["relationships"] if r["relationshipType"]=="DEPENDS_ON"]; sys.exit(0 if (d["spdxVersion"]=="SPDX-2.3" and d["dataLicense"]=="CC0-1.0" and len(pkgs)==4 and len(rels)==5 and pkgs["examples/sbom_libs/dep_a.hls"]["primaryPackagePurpose"]=="LIBRARY") else 1)';
then
    ok "hls-sbom lists the same tree in SPDX (packages, relationships)"
else
    bad "hls-sbom SPDX document wrong on the demo (rc=$s104s_rc)"
fi
s104_a=$(SOURCE_DATE_EPOCH=1727800000 python3 tools/hls-sbom.py examples/sbom_demo.hls --format cdx --stdout 2>/dev/null)
s104_b=$(SOURCE_DATE_EPOCH=1727800000 python3 tools/hls-sbom.py examples/sbom_demo.hls --format cdx --stdout 2>/dev/null)
if [ -n "$s104_a" ] && [ "$s104_a" == "$s104_b" ]; then
    ok "hls-sbom documents are content-addressed (byte-identical reruns)"
else
    bad "hls-sbom documents are not deterministic across reruns"
fi
s104_pkg=$(python3 tools/hls-sbom.py --pkg examples/pkg_audit_demo --format cdx --stdout 2>/dev/null); s104p_rc=$?
if [ "$s104p_rc" -eq 0 ] \
   && echo "$s104_pkg" | python3 -c 'import json,sys; d=json.load(sys.stdin); comps={c["name"]: c for c in d["components"]}; sys.exit(0 if (d["metadata"]["component"]["name"]=="pkg_audit_demo" and set(comps)=={"audit_lib_a","audit_lib_b"} and comps["audit_lib_b"]["type"]=="library" and comps["audit_lib_b"]["purl"]=="pkg:generic/halis/audit_lib_b@0.1.0") else 1)';
then
    ok "hls-sbom --pkg lists the manifest tree with purls and hashes"
else
    bad "hls-sbom --pkg wrong on the package demo (rc=$s104p_rc)"
fi

# Stage 105: hls-repro, the reproducible-build verification across
# distros. The same tree built twice in two isolated roots under two
# profiles (locale, TZ, umask, hash seed, root-path shape) must give
# byte-identical C and binary; the buildinfo is content-addressed
# (two runs, byte-identical); a shifted verify slice returns the
# bytes. Engine auto: native hlc when built, the boot chain
# otherwise.
s105_rc=0
SOURCE_DATE_EPOCH=1727800000 python3 tools/hls-repro.py \
    examples/repro_demo.hls --builds 2 --json \
    > "$TMP/s105_bi.json" 2>/dev/null || s105_rc=$?
if [ "$s105_rc" -eq 0 ] \
   && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); cs={b["c_sha256"] for b in d["builds"]}; bs={b["binary_sha256"] for b in d["builds"]}; sys.exit(0 if (d["schema"]=="hls-buildinfo/v1" and d["matrix"]["builds"]==2 and d["engine"] in ("boot","native") and len(cs)==1 and len(bs)==1 and d["epoch"]["source"] in ("derived","SOURCE_DATE_EPOCH")) else 1)' \
      "$TMP/s105_bi.json"; then
    ok "hls-repro: two distro profiles, byte-identical C and binary"
else
    bad "hls-repro failed the two-profile recipe (rc=$s105_rc)"
fi
SOURCE_DATE_EPOCH=1727800000 python3 tools/hls-repro.py \
    examples/repro_demo.hls --builds 2 --json \
    > "$TMP/s105_bi2.json" 2>/dev/null
if cmp -s "$TMP/s105_bi.json" "$TMP/s105_bi2.json"; then
    ok "hls-repro buildinfo is content-addressed (byte-identical reruns)"
else
    bad "hls-repro buildinfo is not deterministic across reruns"
fi
s105v_rc=0
python3 tools/hls-repro.py --verify "$TMP/s105_bi.json" >/dev/null \
    2>&1 || s105v_rc=$?
if [ "$s105v_rc" -eq 0 ]; then
    ok "hls-repro --verify: a shifted profile slice returns the bytes"
else
    bad "hls-repro --verify failed (rc=$s105v_rc)"
fi
# The tampered buildinfo must refuse: one flipped hash is a lie the
# verifier will not sign.
python3 - "$TMP/s105_bi.json" "$TMP/s105_bad.json" << 'S105EOF'
import json, sys
d = json.load(open(sys.argv[1]))
d["outputs"]["c_sha256"] = "0" * 64
json.dump(d, open(sys.argv[2], "w"))
S105EOF
s105t_rc=0
python3 tools/hls-repro.py --verify "$TMP/s105_bad.json" >/dev/null \
    2>&1 || s105t_rc=$?
if [ "$s105t_rc" -eq 1 ]; then
    ok "hls-repro --verify refuses a tampered buildinfo (exit 1)"
else
    bad "hls-repro --verify accepted a tampered buildinfo (rc=$s105t_rc)"
fi

# Stage 106: hls-sign, the signed packages (minisign, ed25519). The
# primitives prove themselves against the RFC vectors (the tool's own
# selftest); a payload signs and verifies; a flipped byte and a
# foreign key refuse. The full battery is the sign-acceptance gate;
# this pins the pipeline is wired.
s106_rc=0
python3 tools/hls-sign.py selftest > "$TMP/s106_self.txt" 2>/dev/null \
    || s106_rc=$?
if [ "$s106_rc" -eq 0 ] && [ "$(grep -c '  ok: ' "$TMP/s106_self.txt")" -eq 10 ]; then
    ok "hls-sign selftest: the RFC 8032 + RFC 8439 vectors all pass"
else
    bad "hls-sign selftest failed (rc=$s106_rc)"
fi
echo "release payload 106" > "$TMP/s106_payload.txt"
HLS_SIGN_PASSWORD=gate-106 python3 tools/hls-sign.py keygen \
    -f "$TMP/s106.key" -p "$TMP/s106.pub" --iterations 1000 \
    > /dev/null 2>&1 \
    && HLS_SIGN_PASSWORD=gate-106 python3 tools/hls-sign.py sign \
        "$TMP/s106_payload.txt" -s "$TMP/s106.key" \
        --trusted-comment "timestamp:0" > /dev/null 2>&1 \
    && HLS_SIGN_PASSWORD=gate-106 python3 tools/hls-sign.py verify \
        "$TMP/s106_payload.txt" -p "$TMP/s106.pub" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    ok "hls-sign: keygen -> sign -> verify round trip (exit 0)"
else
    bad "hls-sign round trip failed"
fi
echo "release payload 10!" > "$TMP/s106_payload.txt"
if HLS_SIGN_PASSWORD=gate-106 python3 tools/hls-sign.py verify \
        "$TMP/s106_payload.txt" -p "$TMP/s106.pub" > /dev/null 2>&1; then
    bad "hls-sign accepted a tampered payload"
else
    ok "hls-sign refuses a tampered payload (exit 1)"
fi

# Stage 108: hls-reverify, the memory-safety re-verification under
# -O fast (proof replay). The tool re-derives every elided check's
# proof on the OPTIMISED IR and reconciles it with the checker's
# baseline under a fail-closed law; the demos replay OK, the report
# is deterministic, and the whole positive corpus re-proves.
s108_rc=0
python3 tools/hls-reverify.py selftest > "$TMP/s108_self.txt" 2>/dev/null \
    || s108_rc=$?
if [ "$s108_rc" -eq 0 ] && grep -q "40 passed / 0 failed" "$TMP/s108_self.txt"; then
    ok "hls-reverify selftest: 40 vectors (arithmetic, widening, law, audit)"
else
    bad "hls-reverify selftest failed (rc=$s108_rc)"
fi
s108_a=$(python3 tools/hls-reverify.py tests/ok/feat_stage108_reverify.hls --json 2>/dev/null)
s108_b=$(python3 tools/hls-reverify.py tests/ok/feat_stage108_reverify.hls --json 2>/dev/null)
if [ -n "$s108_a" ] && [ "$s108_a" == "$s108_b" ] \
   && echo "$s108_a" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d["schema"]=="hls-reverify-report/v1" and d["verdict"]=="REPLAY-OK" and d["baseline"]["proven"]>=5) else 1)'; then
    ok "hls-reverify: the ok-test's claims replay OK, byte-identical reruns"
else
    bad "hls-reverify wrong on the stage ok-test"
fi
s108_demo=$(python3 tools/hls-reverify.py examples/hmac_proven.hls 2>/dev/null); s108d_rc=$?
if [ "$s108d_rc" -eq 0 ] && echo "$s108_demo" | grep -q "verdict: REPLAY-OK"; then
    ok "hls-reverify: the HMAC demo's elisions re-prove after the optimiser"
else
    bad "hls-reverify failed on hmac_proven (rc=$s108d_rc)"
fi

# Stage 111: capability tokens. Every feat_stage111 program must be
# byte-identical interpreter vs native (default AND -O fast), the
# fail-side rules must stay rejected, and the demo must run green on
# both front-ends.
for f in tests/ok/feat_stage111_*.hls; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .hls)
    if [ ! -x "$TMP/hlc1" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
    fi
    interp_out=$(timeout 60 python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_rc=$?
    if "$TMP/hlc1" "$f" "$TMP/$name.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm -pthread 2>/dev/null; then
        nat_out=$(timeout 60 "$TMP/$name.bin" </dev/null 2>/dev/null); nat_rc=$?
        if [ "$interp_out" == "$nat_out" ] && [ "$interp_rc" == "$nat_rc" ]; then
            ok "$name (native matches interpreter)"
        else
            bad "$name (diverges: interp=$interp_rc nat=$nat_rc)"
        fi
    else
        bad "$name (native compile failed)"
    fi
    if "$TMP/hlc1" --fast "$f" "$TMP/$name.fast.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.fast.bin" "$TMP/$name.fast.c" -lm -pthread 2>/dev/null; then
        fast_out=$(timeout 60 "$TMP/$name.fast.bin" </dev/null 2>/dev/null); fast_rc=$?
        if [ "$interp_out" == "$fast_out" ] && [ "$interp_rc" == "$fast_rc" ]; then
            ok "$name (-O fast matches interpreter)"
        else
            bad "$name (-O fast diverges: interp=$interp_rc fast=$fast_rc)"
        fi
    else
        bad "$name (fast compile failed)"
    fi
done
# The fail-side authority rules must hold on the native front-end too:
# every fail_stage111 program is refused by hlc with its reason.
cap_fails=0
for f in tests/fail/fail_stage111_*.hls; do
    [ -f "$f" ] || continue
    if [ ! -x "$TMP/hlc1" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
    fi
    if "$TMP/hlc1" "$f" "$TMP/cap_fail.c" >/dev/null 2>&1; then
        bad "$(basename "$f" .hls) (hlc accepted a capability violation)"
        cap_fails=$((cap_fails+1))
    fi
done
if [ "$cap_fails" -eq 0 ]; then
    ok "fail_stage111_*: hlc refuses every capability violation"
fi

# Stage 98: refinement types. Every refined slot is guarded at runtime
# (fn entry / return / let / assign / construction); proven sites are
# elided. The interpreter and the native build (default AND -O fast)
# must agree byte for byte, including the runtime-violation panics.
for f in tests/ok/feat_stage98_*.hls; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .hls)
    if [ ! -x "$TMP/hlc1" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
    fi
    interp_out=$(timeout 60 python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_rc=$?
    if "$TMP/hlc1" "$f" "$TMP/$name.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm -pthread 2>/dev/null; then
        nat_out=$(timeout 60 "$TMP/$name.bin" </dev/null 2>/dev/null); nat_rc=$?
        if [ "$interp_out" == "$nat_out" ] && [ "$interp_rc" == "$nat_rc" ]; then
            ok "$name (native matches interpreter)"
        else
            bad "$name (diverges: interp=$interp_rc nat=$nat_rc)"
        fi
    else
        bad "$name (native compile failed)"
    fi
    if "$TMP/hlc1" --fast "$f" "$TMP/$name.fast.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.fast.bin" "$TMP/$name.fast.c" -lm -pthread 2>/dev/null; then
        fast_out=$(timeout 60 "$TMP/$name.fast.bin" </dev/null 2>/dev/null); fast_rc=$?
        if [ "$interp_out" == "$fast_out" ] && [ "$interp_rc" == "$fast_rc" ]; then
            ok "$name (-O fast matches interpreter)"
        else
            bad "$name (-O fast diverges: interp=$interp_rc fast=$fast_rc)"
        fi
    else
        bad "$name (fast compile failed)"
    fi
done
# The refinements must compose into a proof: ratio()'s division (a
# non-negative dividend, a non-zero divisor) has its panic check
# elided under -O fast, while every runtime guard stays.
if "$TMP/hlc1" --fast examples/refine_demo.hls "$TMP/refdem.c" >/dev/null 2>&1; then
    if grep -q "return (u_num / u_den);" "$TMP/refdem.c" \
       && grep -q "refinement violation: parameter 'amount' of 'apply'" "$TMP/refdem.c" \
       && grep -q "refinement violation: field 'Ledger.funds'" "$TMP/refdem.c"; then
        ok "refine_demo: proven division elided, runtime guards kept (-O fast)"
    else
        bad "refine_demo: elision pattern wrong in -O fast C"
    fi
else
    bad "refine_demo: -O fast compile failed"
fi

# hlmodel must exhaustively check the demo state machine.
if python3 tools/hlmodel.py examples/conn_machine.hls --fn step --invariant all_valid --init Closed >/dev/null 2>&1; then
    ok "hlmodel exhaustive check of the demo state machine"
else
    bad "hlmodel failed on the demo state machine"
fi
# Runtime contract checking (--contracts) must catch a violated requires.
cat > "$TMP/rt_contract.hls" <<'RTSEOF'
fn risky(n: int) -> int
    requires n > 0
{
    return n * 2
}
fn main() -> int uses Args {
    let n: int = args().len() - 1
    return risky(n)
}
RTSEOF
rt_out=$(python3 boot/boot.py --contracts "$TMP/rt_contract.hls" </dev/null 2>&1 >/dev/null); rt_rc=$?
if [ $rt_rc -eq 101 ] && echo "$rt_out" | grep -q "contract violation: requires"; then
    ok "--contracts runtime requires violation panics cleanly (101)"
else
    bad "--contracts did not catch the violated requires (rc=$rt_rc)"
fi
# Stage-17 perfection (v0.30.0-alpha): the NATIVE backend now checks
# ENSURES at every return too (previously requires-only). Both
# implementations must panic identically on a violated postcondition.
cat > "$TMP/rt_ens.hls" <<'ENSEOF'
fn bad(x: int) -> int
    requires x > 0
    ensures result > 100
{
    return x
}
fn main() -> int uses IO {
    let a: int = bad(5)
    println("a=" + a.to_str())
    return 0
}
ENSEOF
ens_interp=$(python3 boot/boot.py --contracts "$TMP/rt_ens.hls" </dev/null 2>&1); ens_irc=$?
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" --contracts "$TMP/rt_ens.hls" "$TMP/rt_ens.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/rt_ens.bin" "$TMP/rt_ens.c" -lm -pthread 2>/dev/null; then
    ens_nat=$("$TMP/rt_ens.bin" </dev/null 2>/dev/null); ens_nrc=$?
    if [ $ens_irc -eq 101 ] && [ $ens_nrc -eq 101 ] \
            && echo "$ens_interp" | grep -q "contract violation: ensures of 'bad'"; then
        ok "native --contracts ensures violation panics identically (101)"
    else
        bad "native ensures check diverged (interp=$ens_irc nat=$ens_nrc)"
    fi
else
    bad "native --contracts ensures compile failed"
fi

echo "=== 4b. Stage 14 release: tooling (hlfmt idempotent + hllint cfaware) ==="
# hlfmt must be idempotent: running twice = running once. We test by
# formatting once into a temp file, then formatting again into a second
# temp file, and diffing them. (The original source files may predate
# hlfmt's canonical form, so checking `hlfmt -c` against the original
# is too strict.)
fmt_fail=0
for f in examples/*.hls tests/ok/*.hls tests/fail/*.hls; do
    name=$(basename "$f" .hls)
    cp "$f" "$TMP/p1.hls"
    python3 tools/hlfmt.py -w "$TMP/p1.hls" >/dev/null 2>&1
    cp "$TMP/p1.hls" "$TMP/p2.hls"
    python3 tools/hlfmt.py -w "$TMP/p2.hls" >/dev/null 2>&1
    if diff -q "$TMP/p1.hls" "$TMP/p2.hls" >/dev/null 2>&1; then
        ok "hlfmt idempotent: $name"
    else
        bad "hlfmt non-idempotent: $name"
        fmt_fail=1
    fi
done
# hllint L005 control-flow-aware: 3 warnings expected on the cfaware test.
l005_out=$(python3 tools/hllint.py --rule L005 tests/ok/feat_lint_cfaware.hls 2>&1)
l005_count=$(echo "$l005_out" | grep -c "L005" || true)
if [ "$l005_count" -eq 3 ]; then
    ok "hllint L005 cfaware: 3 warnings (unsafe_unwrap, unsafe_in_loop, unwrap_literal)"
else
    bad "hllint L005 cfaware: expected 3 warnings, got $l005_count"
    echo "$l005_out"
fi
# hllint must NOT warn on safe unwraps (cases 2 and 3 in the test file).
if echo "$l005_out" | grep -q "safe_unwrap\|safe_unwrap_option"; then
    bad "hllint L005 cfaware: false positive on a safe unwrap"
else
    ok "hllint L005 cfaware: no false positives on safe unwraps"
fi


echo "=== 4c. deep-scan-29: Stage 32 bench driver sanity ==="
# The bench-stdlib gate was fully red before deep-scan-29: 53 functions
# failed the driver's `uses IO`-only type-check (any callee requiring
# Net/Rand/Proc/Conc) and 54 panicked on generic default inputs. Lock
# in the three fix classes with one representative function each:
#   mutex_new  — Conc effect      (was: hlc type error)
#   env_var    — Proc effect      (was: hlc type error)
#   base64_decode — curated valid input (was: run panic on "hello world")
# All three must be MEASURED (not skipped, not failed) at low iters.
bench_out=$(python3 tools/hls-bench.py --iters 3000 \
    --only mutex_new,env_var,base64_decode 2>&1); bench_rc=$?
bench_measured=$(echo "$bench_out" | grep -E "^[0-9]+ measured" | grep -oE "^[0-9]+")
if [ $bench_rc -eq 0 ] && [ "$bench_measured" -eq 3 ]; then
    ok "hls-bench driver: effects + curated inputs (3/3 measured, 0 failed)"
else
    bad "hls-bench driver: rc=$bench_rc measured=$bench_measured (want 3)"
    echo "$bench_out" | tail -6
fi
# Skipped-only selections must report cleanly, not crash.
if python3 tools/hls-bench.py --iters 3000 --only csrf_generate_token >/dev/null 2>&1; then
    bad "hls-bench: skip-only selection should exit non-zero"
else
    ok "hls-bench: skip-only selection reports 'no functions to benchmark'"
fi
# ll_validate --help must print usage (was: 'cannot read: --help' error).
if python3 tools/ll_validate.py --help >/dev/null 2>&1; then
    ok "ll_validate: --help prints usage"
else
    bad "ll_validate: --help broken"
fi
