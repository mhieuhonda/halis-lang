#!/usr/bin/env bash
# ============================================================================
# run_tests.sh — the full suite driver.
# ----------------------------------------------------------------------------
# The eight differential suites are Halis gates now (tests/acc/
# suite_XX_*_acceptance.hls, compiled by bin/hlc): this driver builds
# each gate once and runs it, letting the gates print their own
# per-leg [OK]/[FAIL] grammar and keeping the suite exit law — the
# run is green only when every gate is.
#
# The gates run in lanes, JOBS at a time: each gate is an independent
# child tree writing only under its own tests/.acc-scratch/<gate>/
# world, so overlapping them changes no law. Every gate's stdout is
# captured to its own file and replayed in GATE ORDER — a finished
# gate holds its block until every earlier gate has emitted — so the
# bytes on stdout are exactly the sequential run's bytes: the log
# stays diffable, tee-able and greppable, and the parallelism is
# invisible to everything but the clock.
#
# JOBS defaults to 1 — the canonical sequential law. The corpus is
# not perfectly lane-safe yet (feat_stage38_http's in-process accept
# is sensitive to external CPU load), so opt in (JOBS=4 on a big CI
# runner) only when your machine has cores to spare.
#
# Two artifacts are built once here and exported to every gate:
#   HL_TEST_HLC1    bin/acc-hlc1     the native compiler (gcc on the
#                                    committed snapshot) every gate's
#                                    build_hlc1 would otherwise ignite
#                                    the interpreter for, and
#   HL_TEST_HLC_SRC bin/hlc1_src.c   the Stage-0 emission itself (one
#                                    interpreter self-compile), the
#                                    reference side of the determinism
#                                    legs in suites 03/05/07.
# Standalone gate runs (no vars) build their own, the old way.
# ============================================================================
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0

# The canonical eight, overridable (GATES="suite_03_selfhost" for a
# focused run / driver self-test).
if [ -n "${GATES:-}" ]; then
    # shellcheck disable=SC2206
    GATES=(${GATES})
else
    GATES=(
        suite_01_boot
        suite_02_differential
        suite_03_selfhost
        suite_04_optimizers
        suite_05_backends
        suite_06_advanced
        suite_07_asm
        suite_08_osdev
    )
fi

# JOBS defaults to 1 (the canonical sequential battery). The build
# phase parallelises at NCPU regardless (make -j): compiling C is pure
# CPU work with no test logic in it, so it is safe at any width.
JOBS="${JOBS:-1}"
NCPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
if [ "$NCPU" -gt 8 ]; then NCPU=8; fi
if [ "$NCPU" -lt 1 ]; then NCPU=1; fi
BUILD_JOBS="${BUILD_JOBS:-$NCPU}"

# ---- 1. build phase: every gate, once, in parallel -------------------------
# A missing bin/acc-<gate> is filled by make -j here (the per-gate
# fallback inside launch stays for a drifted tree, the way the old
# driver had it).
BUILD_ARGS=()
for gate in "${GATES[@]}"; do
    BUILD_ARGS+=("bin/acc-$gate")
done
make -s -j"$BUILD_JOBS" "${BUILD_ARGS[@]}" >/dev/null 2>&1 || true

# ---- 1b. one shared native compiler for every gate --------------------------
# Every gate's build_hlc1 wants the same artifact: the native compiler.
# Building it the interpreter-driven way costs a ~80s Stage-0 self-compile
# PER GATE for bytes the fixed-point law already pins: snapshot/hlc.c IS
# the interpreter's own emission for src/hlc.hls (make bootstrap refuses
# any drift, and suite_01 re-proves the chain). One gcc ignition here and
# an HL_TEST_HLC1 export replaces seven of those ignitions with one; a
# gate run standalone (no var set) builds its own the old way.
SHARED_HLC1=bin/acc-hlc1
make -s "$SHARED_HLC1" >/dev/null 2>&1 || true
if [ -x "$SHARED_HLC1" ]; then
    export HL_TEST_HLC1="$SHARED_HLC1"
fi

# ---- 1c. one shared Stage-0 emission for the determinism legs ---------------
# The same fixed-point law, the other direction: the interpreter's own
# emission of src/hlc.hls, built ONCE here and exported as HL_TEST_HLC_SRC.
# The determinism legs (suite_03's fixed point, suite_05's neon parity,
# suite_07's three reruns) used to pay a full interpreter self-compile per
# side — now each keeps one fresh in-gate pass and cmp's it against this
# file. make caches the artifact, so repeat runs only re-emit when the
# compiler source actually moved.
SHARED_HLC_SRC=bin/hlc1_src.c
make -s "$SHARED_HLC_SRC" >/dev/null 2>&1 || true
if [ -f "$SHARED_HLC_SRC" ]; then
    export HL_TEST_HLC_SRC="$SHARED_HLC_SRC"
fi

# ---- 2. run phase: lanes over the gates, output replayed in gate order -----
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT INT TERM HUP

declare -a PID_OF=()    # lane pid -> gate index
declare -a GSTATE=()      # per gate: 0 pending, 1 pass, 2 fail
declare -a GSTART=()      # per gate: $SECONDS when its lane fired
declare -a GELAPSED=()    # per gate: wall seconds, reported in the summary
for i in "${!GATES[@]}"; do GSTATE[$i]=0; GELAPSED[$i]=0; done
NEXT=0
EMIT=0

launch() {
    # $1 = gate index. Starts the gate in the background, its stdout
    # captured to $SCRATCH/<i>.out; the verdict lands in $SCRATCH/<i>.rc.
    # A gate that cannot even build fails its lane HERE, in the
    # foreground bookkeeping, with the old driver's exact words.
    local i="$1"
    local gate="${GATES[$1]}"
    GSTART[$i]=$SECONDS
    if [ ! -x "bin/acc-$gate" ]; then
        if ! make -s "bin/acc-$gate" >/dev/null 2>&1; then
            echo "  [FAIL] $gate (gate build failed)" > "$SCRATCH/$i.out"
            echo 2 > "$SCRATCH/$i.rc"
            GSTATE[$i]=2
            return 0
        fi
    fi
    ( "bin/acc-$gate" > "$SCRATCH/$i.out" 2>&1; echo $? > "$SCRATCH/$i.rc" ) &
    PID_OF[$!]=$i
}

# Fire the first JOBS lanes.
for lane in $(seq 1 "$JOBS"); do
    [ "$NEXT" -ge "${#GATES[@]}" ] && break
    launch "$NEXT"
    NEXT=$((NEXT + 1))
done

# Drain: block until some lane finishes, take its verdict, fire the
# next gate into the freed lane. wait -n reaps exactly one dead child
# per call; the kill -0 scan then finds that one pid (a zombie is
# still kill -0 alive, so a lane is never reaped twice).
while [ "${#PID_OF[@]}" -gt 0 ]; do
    wait -n 2>/dev/null
    for pid in "${!PID_OF[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then continue; fi
        wait "$pid" 2>/dev/null
        gi="${PID_OF[$pid]}"
        rc=$(cat "$SCRATCH/$gi.rc" 2>/dev/null || echo 1)
        GELAPSED[$gi]=$((SECONDS - ${GSTART[$gi]:-0}))
        if [ "$rc" -eq 0 ]; then GSTATE[$gi]=1; else GSTATE[$gi]=2; fi
        unset "PID_OF[$pid]"
        if [ "$NEXT" -lt "${#GATES[@]}" ]; then
            launch "$NEXT"
            NEXT=$((NEXT + 1))
        fi
    done
done

# Replay in gate order: a finished gate holds its block until every
# earlier gate has emitted, so the log is byte-identical to the
# sequential run's.
while [ "$EMIT" -lt "${#GATES[@]}" ]; do
    if [ ! -f "$SCRATCH/$EMIT.rc" ]; then break; fi
    cat "$SCRATCH/$EMIT.out"
    EMIT=$((EMIT + 1))
done
# Anything still without a verdict (should not happen; the launch
# foreground path writes .rc immediately) is a failure by name.
while [ "$EMIT" -lt "${#GATES[@]}" ]; do
    echo "  [FAIL] ${GATES[$EMIT]}"
    GSTATE[$EMIT]=2
    EMIT=$((EMIT + 1))
done

for i in "${!GATES[@]}"; do
    if [ "${GSTATE[$i]}" -eq 1 ]; then PASS=$((PASS + 1));
    elif [ "${GSTATE[$i]}" -eq 2 ]; then FAIL=$((FAIL + 1)); fi
done

# Per-gate wall time, appended after the verdict so the replayed gate
# blocks above stay byte-identical to the sequential run's log.
echo "------------------------------------------"
for i in "${!GATES[@]}"; do
    printf "  %-26s %5ss  %s\n" "${GATES[$i]}" "${GELAPSED[$i]}" \
        "$([ "${GSTATE[$i]}" -eq 1 ] && echo ok || echo FAIL)"
done
echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL (suite gates)"
echo "=========================================="
[ $FAIL -eq 0 ]
