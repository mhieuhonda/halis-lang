#!/usr/bin/env python3
"""hls-xdiff — the Stage 133 cross-implementation differential suite.

Usage:
  python3 tools/hls-xdiff.py                     # the default sweep over the purity-gated corpus
  python3 tools/hls-xdiff.py --core              # the curated core corpus (fast; every verdict class)
  python3 tools/hls-xdiff.py --file <prog.hls>   # one program, every available backend
  python3 tools/hls-xdiff.py --backends interp,native,wasm
  python3 tools/hls-xdiff.py --discover          # the purity census, no legs run
  python3 tools/hls-xdiff.py --list              # print the corpus, no legs run
  python3 tools/hls-xdiff.py --json report.json  # machine-readable report
  python3 tools/hls-xdiff.py --update-ledger     # draft waiver entries for the diverging set
  python3 tools/hls-xdiff.py selftest            # normalizer + verdict vectors, no backends

Stage 133's question is the one a multi-backend language owes an honest
answer to forever: does the SAME program behave the SAME way no matter
which implementation executes it? The project ships four implementations
of the language — the Stage-0 interpreter (boot/), the self-hosted C
backend (hlc -> C -> gcc), the WebAssembly backend (tools/hlwasm.py) and
the LLVM IR emitter (boot --emit llvm) — and until now their agreement
was only ever checked pairwise, piecemeal, in different suites: the
interpreter met the native build in run_tests.sh, the wasm backend met
hello.hls in the backend suite, the LLVM IR met a structural validator.
Nobody ever sat the implementations down at the same table with the
same program and made them show their work. This tool is that table.

The backends (four registered, three executing):

  interp  boot/boot.py <file>                    the reference implementation
  native  bin/hlc <file> <c> + gcc -O2 + run     the shipped compiler
  wasm    tools/hlwasm.py <file> + node          the wasm32 backend
  llvm    boot/boot.py --emit llvm + ll_validate STATIC — structural validation
          (+ llvm-as when installed); the llvm leg never executes here,
          it participates in the report as a validated static leg and
          the report says so, in those words.

The oracle. Two channels are compared across every pair of executing
legs: the program's stdout BYTES and its exit code.

  stdout     exact byte equality is the goal. Where backends are known
             to differ only in REPRESENTATION — C's printf renders
             0.75 as "0.750000" while the wasm glue's JS Number renders
             "0.75"; C prints "inf"/"-nan" where JS prints
             "Infinity"/"NaN" — the channel canonicalizer maps both
             sides onto one numeric form (round to the printf channel's
             6 decimal places, shortest-roundtrip repr, exponent forms
             normalized, +/-nan folded to nan, the sign of zero
             dropped) and the verdict records `normalized` instead of
             `exact`. Short decimal forms ("1.5"), version strings
             ("1.10") and integers ("9007199254740993") are NEVER
             rewritten: a divergence between those is real, and the
             canonicalizer refuses to launder it.
  exit code  equal codes compare exactly. Where a backend HALTS, the
             halt classes are compared as classes: interp/native panic
             with 101; the wasm alpha halts every panic/abort/exit(N)
             through one `unreachable` trap (the emitter's documented
             alpha convention: a visible halt beats a silent wrong
             answer — exit codes are not propagated). So:
               all legs exit 0                      -> exact/normalized
               hosted legs all 101, wasm trapped    -> panic-equal
               hosted legs uniform N, wasm trapped  -> halt-gap (named)
               one side clean, another halted       -> divergence (ALARM)
               any other exit disagreement          -> divergence (ALARM)
             stderr is recorded but never compared — panic WORDING is
             backend-owned; that a halt happened, and that the bytes
             before it agree, is what the oracle owns.

The verdict lattice (per program): exact | normalized | panic-equal |
halt-gap | unpaired (only one executing leg — nothing to compare) |
skipped (no executing leg) | divergence | error. The run is GREEN when
there is no unexplained divergence and no unledgered error, and the
ledger is consistent (see below); `--strict` demotes `normalized` and
`halt-gap` to failures for auditing the byte-exact frontier.

The ledger. A divergence the suite discovers is a FINDING, not a
failure to be swept: the first sweep (v0.151.0-alpha) found six real
cross-implementation divergences, each now named in
tests/xdiff/ledger.json with its diagnosis — two wasm emitter bugs that
produce invalid modules (nested-generic calls, tail-call lowering), one
documented unimplemented builtin (str.to_float halts), one wrong f64
comparison at the 2^63 corner, and two missing soundness checks (the
runtime refinement guard and the byte_at bounds check are not emitted
on wasm: the interp panics 101, wasm runs on and prints output the
other backends never print). The ledger is two-way,
like Stage 128's: a divergence the ledger names is SANCTIONED (the run
stays green, the diagnosis is quoted back); a ledgered program that now
compares clean is STALE (trim the ledger); a ledgered program whose
observed per-backend states no longer match the entry is a REGRESSION
(the divergence moved — re-diagnose); and an unledgered divergence is
the alarm this stage exists to wire. `--update-ledger` drafts entries;
the tool never writes the file.

The purity gate. A program is differential-eligible only when its
output is a function of its source: crate modes (#![freestanding] and
friends) have no hosted main; imports of time/env/net/fs/thread/...
(read the gate's deny table) make output environment- or wall-clock-
dependent and channel-bound (a socket program's stdout depends on the
network, not just the implementation). Eligibility is decided by
scanning the SOURCE (imports + impure builtins), the reason for every
exclusion is NAMED in the report, and `--all` lifts the gate for
auditing (an impure program then simply earns whatever its legs earn —
usually an honest timeout error). `--discover` prints the census
without running a single leg.

Corpus: tests/ok (the acceptance corpus every release ships) plus
tests/xdiff (fixtures written FOR the cross-impl table, in the wasm
alpha's supported subset). `--core` runs the curated subset that
exercises every verdict class on every executing backend in minutes;
the default sweep is the whole gated corpus.

Exit codes: 0 = green (every executing pair agreed, or its divergence
is ledgered; the ledger is consistent; at least two executing backends
participated); 1 = not green (an unledgered divergence or error, a
stale or mismatched ledger entry, or no differential pair available);
2 = usage error (unknown path, bad option, no such corpus file).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT = os.path.join(REPO, "boot", "boot.py")
HLWASM = os.path.join(REPO, "tools", "hlwasm.py")
LL_VALIDATE = os.path.join(REPO, "tools", "ll_validate.py")
HLC = os.path.join(REPO, "bin", "hlc")
LEDGER_PATH = os.path.join(REPO, "tests", "xdiff", "ledger.json")
OK_DIR = os.path.join(REPO, "tests", "ok")
XDIFF_DIR = os.path.join(REPO, "tests", "xdiff")

EXECUTING_BACKENDS = ("interp", "native", "wasm")
ALL_BACKENDS = ("interp", "native", "wasm", "llvm")

# ---------------------------------------------------------------------------
# The channel canonicalizer
# ---------------------------------------------------------------------------

# nan / inf tokens, case-insensitive, word-bounded so identifiers and
# version strings never fold.
_NAN_RE = re.compile(r"(?<![\w.])[-+]?nan(?![\w.])", re.IGNORECASE)
_INF_RE = re.compile(r"(?<![\w.])[-+]?inf(?:inity)?(?![\w.])", re.IGNORECASE)

# A float literal: needs a decimal point or an exponent — bare integers
# (ids, counts, 64-bit constants) are never touched.
_FLOAT_RE = re.compile(
    r"(?<![\w.])[-+]?(?:\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+)(?![\w.])"
)


def _canonical_float(tok: str) -> str:
    """One canonical string per float VALUE at the printf channel.

    The C backends observe floats through printf("%f") — six decimal
    places; the wasm glue observes them through JS Number's shortest
    round-trip. The shared observation channel is the printf channel:
    round to 6 decimals, render shortest, normalize exponents, drop
    the sign of zero. Both sides run through this same function, so
    equal doubles yield equal strings and the function's opinions
    never favor a backend.
    """
    try:
        v = float(tok)
    except ValueError:
        return tok
    if v == 0.0:
        return "0.0"
    v = round(v, 6)
    if v == 0.0:
        return "0.0"
    return repr(v)


def _is_channel_float(tok: str) -> bool:
    """Would printf or JS render this token differently from its value?

    printf("%f") always emits six decimals -> any token with three or
    more decimal digits is channel noise. JS emits an exponent below
    1e-7 / above 1e21 -> any exponent token is channel noise. Anything
    with seven or more integer digits renders in full on the printf
    channel and abbreviated in JS. One-decimal/two-decimal short forms
    ("1.5", "3.14") are identical on both channels and stay raw — and
    version strings ("1.10") keep their digits.
    """
    body = tok.lstrip("+-")
    if "e" in body or "E" in body:
        return True
    if "." in body:
        ip, fp = body.split(".", 1)
    else:
        ip, fp = body, ""
    if len(fp) >= 3:
        return True
    if len(ip.lstrip("0")) >= 7:
        return True
    return False


def canonical_channel(text: str):
    """Canonicalize one leg's stdout onto the shared observation channel.

    Returns (canonical_text, changed): changed=True when at least one
    token was rewritten, i.e. the raw bytes carried backend-specific
    representation and only the canonical form is comparable.
    """
    text = _NAN_RE.sub("nan", text)
    text = _INF_RE.sub(lambda m: "-inf" if m.group(0).startswith("-") else "inf", text)

    changed = [False]

    def repl(m):
        tok = m.group(0)
        if not _is_channel_float(tok):
            return tok
        canon = _canonical_float(tok)
        if canon != tok:
            changed[0] = True
        return canon

    return _FLOAT_RE.sub(repl, text), changed[0]


def first_divergence(a: str, b: str):
    """First differing line of two (already canonicalized) outputs."""
    la, lb = a.splitlines(), b.splitlines()
    for i in range(max(len(la), len(lb))):
        va = la[i] if i < len(la) else "<end of output>"
        vb = lb[i] if i < len(lb) else "<end of output>"
        if va != vb:
            return i + 1, va[:80], vb[:80]
    return None


# ---------------------------------------------------------------------------
# The purity gate
# ---------------------------------------------------------------------------

# Crate modes: the program declares itself not-a-hosted-program.
_CRATE_ATTR_RE = re.compile(r"^\s*#!\[\s*([A-Za-z_]+)", re.MULTILINE)

# std modules whose output depends on the wall clock, the environment,
# the filesystem, the network, or the scheduler. A program importing
# any of these cannot be differentially compared byte-for-byte: its
# output is a function of the world, not of the implementation.
_DENY_IMPORTS = {
    "time", "env", "process", "net", "fs", "thread", "sync",
    "http", "http2", "websocket", "sse", "session", "cookie", "csrf",
    "archive", "config", "cli", "complete", "man", "hlscli", "hlsdoc",
    "tui", "color", "progress", "log", "template", "graphql", "openapi",
    "jsffi", "dom", "taint", "sanitize", "uuid", "simd", "repl", "bench",
}
_IMPORT_RE = re.compile(r'^\s*import\s+"(?:std|core)\.([\w]+)', re.MULTILINE)

# Impure builtins: wall clock, randomness, environment, filesystem,
# network, processes, scheduler channels and locks. Same law as above.
_DENY_BUILTIN_RE = re.compile(
    r"\b(rand_seed|rand_int|clock_ms|clock_ns|monotonic_ns|sleep|"
    r"proc_exec|proc_spawn|proc_kill|proc_wait|spawn|"
    r"read_file|write_file|append_file|read_dir|remove_file|"
    r"net_lookup|net_tcp_|net_udp_|hostname|"
    r"env_get|env_set|args_get|read_line|chan_new|chan_send|chan_recv|"
    r"mutex_|taint_unwrap)\b"
)


def purity_verdict(source: str):
    """(eligible, reason). reason names the rule that excluded."""
    m = _CRATE_ATTR_RE.search(source)
    if m:
        return False, "crate mode #![%s] — not a hosted program" % m.group(1)
    for m in _IMPORT_RE.finditer(source):
        mod = m.group(1)
        if mod in _DENY_IMPORTS:
            return False, "imports std.%s — output depends on the world, not the implementation" % mod
    m = _DENY_BUILTIN_RE.search(source)
    if m:
        return False, "builtin '%s' — nondeterministic or environment-bound" % m.group(1)
    return True, None


# ---------------------------------------------------------------------------
# The verdict lattice
# ---------------------------------------------------------------------------

def leg_state(leg):
    """One canonical state token per leg, for the ledger signature."""
    s = leg.get("status")
    if s == "ok":
        return "ok"
    if s == "halted":
        if leg.get("trap"):
            return "halt(trap)"
        return "halt(%d)" % leg.get("exit", -1)
    if s == "error":
        return "error(%s)" % leg.get("kind", "unknown")
    if s == "validated":
        return "validated"
    return "skip"


def compare_program(legs, strict=False):
    """The oracle. legs: backend -> leg dict (status ok/halted/error/skip/
    validated). Returns (verdict, detail).

    SKIP legs are absent by design (the wasm alpha's subset names its
    refusals) and never poison a verdict. ERROR legs are different: a
    backend that TRIED and failed — an invalid module, a timeout — has
    not agreed to anything, so an error leg caps the verdict: the
    program is an `error` (ledger-judgeable) even when the remaining
    pair agrees, because `exact` must mean every backend that could
    run the program ran it and agreed."""
    exec_legs = {b: l for b, l in legs.items() if l.get("status") in ("ok", "halted")}
    errors = {b: l for b, l in legs.items() if l.get("status") == "error"}
    if len(exec_legs) == 0 and not errors:
        return "skipped", "no executing leg"
    if len(exec_legs) + len(errors) < 2:
        only = (list(exec_legs) + list(errors))[0] if (exec_legs or errors) else "?"
        return "unpaired", "only %s ran — nothing to compare" % only

    # divergence among the legs that DID run is the loudest signal
    verdict, detail = None, None
    if len(exec_legs) >= 2:
        verdict, detail = _compare_exec(exec_legs, strict)
        if verdict == "divergence":
            return "divergence", detail

    if errors:
        names = ",".join(sorted(errors))
        why = next(iter(errors.values())).get("why", "")
        return "error", "%s leg failed: %s" % (names, why)

    if verdict is None:
        return "unpaired", "nothing to compare"
    return verdict, detail


def _compare_exec(exec_legs, strict=False):
    halts = {b: l for b, l in exec_legs.items() if l["status"] == "halted"}
    oks = {b: l for b, l in exec_legs.items() if l["status"] == "ok"}

    if oks and halts:
        bad = sorted(halts)
        # A clean run on one backend and a halt on another is the
        # loudest alarm the suite owns: the implementations disagree
        # about whether the program halts at all.
        return "divergence", "halted on %s, ran clean on %s" % (
            ",".join(bad), ",".join(sorted(oks)))

    if halts and not oks:
        hosted = {b: l for b, l in halts.items() if b in ("interp", "native")}
        wasm = halts.get("wasm")
        hosted_codes = sorted({l["exit"] for l in hosted.values()})
        if wasm is not None and wasm.get("trap"):
            if hosted and hosted_codes == [101]:
                return "panic-equal", "every backend halts in the panic class"
            if hosted and len(hosted_codes) == 1:
                return "halt-gap", "every backend halts; wasm does not propagate exit %d (the alpha trap convention)" % hosted_codes[0]
            if hosted:
                return "divergence", "hosted halt codes disagree: %s" % hosted_codes
            return "panic-equal", "wasm halts, no hosted leg available"
        # no trap: deliberate exit(N) on wasm too — codes must agree
        codes = {b: l["exit"] for b, l in halts.items()}
        if len(set(codes.values())) == 1:
            code = list(codes.values())[0]
            if code == 101:
                return "panic-equal", "every executing backend panics (no wasm trap leg)"
            return "exact", "all backends exit %d" % code
        return "divergence", "halt codes disagree: %s" % codes

    # all executing legs ran clean: compare stdout
    raws = {b: l["out"] for b, l in oks.items()}
    if len(set(raws.values())) == 1:
        if strict:
            return "exact", "byte-identical (strict)"
        return "exact", "byte-identical"
    canon = {b: canonical_channel(o.decode("utf-8", "replace"))[0]
             for b, o in raws.items()}
    changed = any(canonical_channel(o.decode("utf-8", "replace"))[1]
                  for o in raws.values())
    if len(set(canon.values())) == 1:
        if changed:
            d = ("agreed on the shared channel; raw bytes differ only in "
                 "float/nan representation")
        else:
            d = "agreed after canonicalization"
        if strict:
            return "divergence", d + " (strict: raw bytes differ)"
        return "normalized", d
    ref = sorted(canon)[0]
    for other in sorted(canon):
        if other == ref:
            continue
        fd = first_divergence(canon[ref], canon[other])
        if fd:
            return "divergence", "line %d: %s=%r vs %s=%r" % (fd[0], ref, fd[1], other, fd[2])
    return "divergence", "outputs differ"


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def load_ledger(path=LEDGER_PATH):
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"schema": 1, "description": "", "waivers": []}, None
    except ValueError as e:
        return None, "corrupt ledger %s: %s" % (path, e)
    if not isinstance(data, dict) or "waivers" not in data:
        return None, "ledger %s has no waivers table" % path
    return data, None


def ledger_judge(name, verdict, detail, legs, ledger):
    """(status, entry): status in sanctioned / stale / mismatch / None."""
    if verdict not in ("divergence", "error"):
        # two-way: an entry naming a program that now compares clean
        # is stale and must be trimmed
        for w in ledger.get("waivers", []):
            if w.get("file") == name:
                return "stale", w
        return None, None
    sig = {b: leg_state(l) for b, l in legs.items()
           if l.get("status") in ("ok", "halted", "error")}
    for w in ledger.get("waivers", []):
        if w.get("file") != name:
            continue
        want = w.get("states", {})
        culprit = w.get("culprit")
        if culprit and culprit not in sig:
            continue  # the named culprit leg did not even run
        if culprit and sig.get(culprit) != want.get(culprit):
            return "mismatch", w
        ok = True
        for b, st in want.items():
            if b == culprit:
                continue
            if b in sig and sig[b] != st:
                ok = False
                break
        if ok:
            return "sanctioned", w
        return "mismatch", w
    return None, None


# ---------------------------------------------------------------------------
# The backends
# ---------------------------------------------------------------------------

_DEVNULL = subprocess.DEVNULL
_RUN_ENV = dict(os.environ, LC_ALL="C", TZ="UTC")


def _run(cmd, timeout, cwd, stdin=_DEVNULL):
    """Run one leg process; return (exit, stdout_bytes, stderr_bytes) or
    raise TimeoutExpired. Output is captured as BYTES: the oracle
    compares bytes, never str() of them."""
    p = subprocess.run(cmd, cwd=cwd, env=_RUN_ENV, stdin=stdin,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def leg_interp(path, scratch, timeout):
    try:
        code, out, err = _run([sys.executable, BOOT, path], timeout, REPO)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout",
                "why": "interpreter exceeded %ss" % timeout}
    if code < 0 or code > 125:
        return {"status": "error", "kind": "crash",
                "why": "interpreter died with signal %d" % -code}
    if code == 1:
        return {"status": "error", "kind": "compile",
                "why": first_line(err)}
    if code == 0:
        return {"status": "ok", "exit": 0, "out": out}
    return {"status": "halted", "exit": code, "out": out, "trap": False,
            "err": tail_line(err)}


def leg_native(path, scratch, timeout):
    if not os.path.exists(HLC):
        return {"status": "skip",
                "why": "bin/hlc missing (run make bootstrap)"}
    if not shutil.which("gcc"):
        return {"status": "skip", "why": "gcc not found"}
    cfile = os.path.join(scratch, "prog.c")
    binfile = os.path.join(scratch, "prog.bin")
    try:
        code, out, err = _run([HLC, path, cfile], timeout, REPO)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout",
                "why": "hlc exceeded %ss" % timeout}
    if code != 0:
        return {"status": "skip", "why": "hlc refused: " + first_line(err)}
    try:
        _run(["gcc", "-O2", "-o", binfile, cfile, "-lm", "-pthread"], timeout, scratch)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout", "why": "gcc timed out"}
    if not os.path.exists(binfile):
        return {"status": "error", "kind": "tool", "why": "gcc produced no binary"}
    try:
        code, out, err = _run([binfile], max(30, timeout // 2), scratch)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout",
                "why": "native run exceeded %ss" % max(30, timeout // 2)}
    if code < 0 or code > 125:
        return {"status": "error", "kind": "crash",
                "why": "native binary died with signal %d" % -code}
    if code == 0:
        return {"status": "ok", "exit": 0, "out": out}
    return {"status": "halted", "exit": code, "out": out, "trap": False,
            "err": tail_line(err)}


_NODE_RUNNER = r"""
const fs = require("fs");
const wasmBytes = fs.readFileSync(process.argv[2]);
eval(fs.readFileSync(process.argv[2].replace(/\.wasm$/, ".js"), "utf-8"));
Halis.run(new Uint8Array(wasmBytes)).then(function (code) {
  process.exit(Number(code) & 0x7fffffff);
}).catch(function (e) {
  console.error("Halis run failed: " + e.message);
  process.exit(1);
});
"""


def leg_wasm(path, scratch, timeout):
    if not shutil.which("node"):
        return {"status": "skip", "why": "node not found"}
    base = os.path.join(scratch, "prog")
    try:
        code, out, err = _run([sys.executable, HLWASM, path, base,
                               "--no-html", "--wasm-opt", "off"], timeout, REPO)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout",
                "why": "hlwasm exceeded %ss" % timeout}
    if code != 0:
        why = first_line(err) or first_line(out)
        # The wasm alpha names its subset refusals: "not yet supported
        # by --emit wasm (Stage 23-alpha subset)", "range() can only
        # be used as the iter expression of a `for` loop in Stage
        # 23-alpha", ... — every one is a SKIP (the backend cannot
        # take the program), never an error.
        if "not yet supported" in why or "Stage 23-alpha" in why:
            return {"status": "skip", "why": "wasm alpha subset: " + why}
        return {"status": "error", "kind": "tool", "why": "hlwasm refused: " + why}
    wasmfile = base + ".wasm"
    if not os.path.exists(wasmfile):
        return {"status": "error", "kind": "tool", "why": "hlwasm wrote no .wasm"}
    runner = os.path.join(scratch, "runner.js")
    with open(runner, "w") as f:
        f.write(_NODE_RUNNER)
    try:
        code, out, err = _run(["node", runner, wasmfile],
                              max(30, timeout // 2), scratch)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout",
                "why": "node run exceeded %ss" % max(30, timeout // 2)}
    errtext = err.decode("utf-8", "replace")
    # The glue's catch receives BOTH runtime traps and module-compile
    # rejections; they are different failure classes and the ledger
    # names them differently. A compile rejection is an emitter bug
    # (invalid module); "unreachable" and friends are halts.
    if "WebAssembly.compile" in errtext or "Compiling function" in errtext:
        return {"status": "error", "kind": "invalid-module",
                "why": first_line(err)}
    if "Halis run failed" in errtext:
        return {"status": "halted", "exit": code, "out": out, "trap": True,
                "err": tail_line(err)}
    if code != 0 or "Error" in errtext:
        kind = "invalid-module" if "WebAssembly.compile" in errtext else "node"
        return {"status": "error", "kind": kind, "why": first_line(err)}
    if code == 0:
        return {"status": "ok", "exit": 0, "out": out}
    return {"status": "halted", "exit": code, "out": out, "trap": False,
            "err": tail_line(err)}


def leg_llvm(path, scratch, timeout):
    """The STATIC leg: emit LLVM IR and structurally validate it.
    The llvm leg never executes here — the report says so, in those
    words. When llvm-as is installed the IR is also assembled, which
    upgrades the structural check to a real parser check."""
    llfile = os.path.join(scratch, "prog.ll")
    try:
        code, out, err = _run([sys.executable, BOOT, "--emit", "llvm", path],
                              timeout, REPO)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout",
                "why": "llvm emit exceeded %ss" % timeout}
    if code != 0:
        why = first_line(err)
        if "not supported" in why or "subset" in why:
            return {"status": "skip", "why": "llvm emitter subset: " + why}
        return {"status": "error", "kind": "compile", "why": "llvm emit: " + why}
    with open(llfile, "wb") as f:
        f.write(out)
    notes = []
    validator = [sys.executable, LL_VALIDATE, llfile]
    try:
        code, vout, verr = _run(validator, timeout, REPO)
    except subprocess.TimeoutExpired:
        return {"status": "error", "kind": "timeout", "why": "ll_validate timed out"}
    if code != 0:
        return {"status": "error", "kind": "invalid-ir",
                "why": "ll_validate: " + first_line(verr)}
    notes.append("IR valid")
    llvm_as = shutil.which("llvm-as")
    if llvm_as:
        try:
            code, aout, aerr = _run([llvm_as, llfile, "-o",
                                     os.path.join(scratch, "prog.bc")],
                                    timeout, scratch)
            if code == 0:
                notes.append("assembled")
            else:
                notes.append("llvm-as rejected: " + first_line(aerr))
        except subprocess.TimeoutExpired:
            notes.append("llvm-as timed out")
    return {"status": "validated", "notes": ", ".join(notes)}


LEGS = {"interp": leg_interp, "native": leg_native, "wasm": leg_wasm,
        "llvm": leg_llvm}


def first_line(b):
    if not b:
        return ""
    line = b.decode("utf-8", "replace").strip().splitlines()
    return (line[0][:160] if line else "")


def tail_line(b):
    if not b:
        return ""
    lines = b.decode("utf-8", "replace").strip().splitlines()
    return (lines[-1][:200] if lines else "")


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

# The curated core: every verdict class on every executing backend, in
# minutes. Sixteen programs the first sweep proved byte-identical on
# all three executing legs; ten the sweep proved panic-equal; two the
# sweep proved channel-normalized; five the sweep found divergent and
# the ledger names; the seven tests/xdiff fixtures pin the classes the
# corpus does not reach on its own.
CORE_CORPUS = [
    # exact, three executing legs
    "feat_contract_basic", "feat_contract_ensures", "feat_contract_loop",
    "feat_deep_scan11_cr_comment", "feat_deep_scan12_lint_deadcode",
    "feat_deep_scan12_lint_unused", "feat_deep_scan12_optimiser",
    "feat_inline_licm", "feat_optimize", "feat_proof_elide",
    "feat_stage101_sidechannel", "feat_stage102_consttime",
    "feat_stage124_bench", "feat_stage130_deprecated",
    "feat_stage97_invariants", "feat_stage98_refine_fast",
    # panic-equal, three executing legs
    "feat_deep_scan25_checked_casts", "feat_proof_sound_divcorner",
    "feat_proof_sound_nz", "feat_proof_sound_ovf",
    "feat_stage98_refine_sound", "panic_badint", "panic_divzero",
    "panic_float_inf_toint", "panic_message", "panic_overflow",
    # channel-normalized
    "feat_arith", "feat_deep_scan6_lexer",
    # ledgered divergences
    "feat_deep_scan7_to_float", "feat_deep_scan27_nested_generic",
    "feat_stage31_tail", "feat_stage98_refine_panic", "panic_str_bounds",
]


def discover_corpus(gated=True):
    """(entries, skips): eligible programs + named exclusions."""
    entries, skips = [], []
    seen = set()
    for d in (XDIFF_DIR, OK_DIR):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".hls") or fn in seen:
                continue
            seen.add(fn)
            path = os.path.join(d, fn)
            if gated:
                try:
                    with open(path, "r", errors="replace") as f:
                        src = f.read()
                except OSError as e:
                    skips.append((fn, "unreadable: %s" % e))
                    continue
                ok, reason = purity_verdict(src)
                if not ok:
                    skips.append((fn, reason))
                    continue
            entries.append((fn[:-4], path))
    return entries, skips


def resolve_core():
    out = []
    for name in CORE_CORPUS:
        p = os.path.join(OK_DIR, name + ".hls")
        if os.path.exists(p):
            out.append((name, p))
    for fn in sorted(os.listdir(XDIFF_DIR)) if os.path.isdir(XDIFF_DIR) else []:
        if fn.endswith(".hls"):
            out.append((fn[:-4], os.path.join(XDIFF_DIR, fn)))
    return out


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def run_program(item, backends, timeout, ledger, strict=False):
    name, path = item
    legs = {}
    for b in backends:
        scratch = tempfile.mkdtemp(prefix="hls-xdiff-")
        try:
            legs[b] = LEGS[b](path, scratch, timeout)
        except Exception as e:  # a leg must never take the suite down
            legs[b] = {"status": "error", "kind": "tool",
                       "why": "%s leg raised: %s" % (b, e)}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    verdict, detail = compare_program({b: legs[b] for b in backends}, strict=strict)
    status, entry = ledger_judge(name, verdict, detail, legs, ledger)
    sig = {b: leg_state(l) for b, l in legs.items()
           if l.get("status") in ("ok", "halted", "error", "validated")}
    rec = {
        "name": name, "path": os.path.relpath(path, REPO),
        "verdict": verdict, "detail": detail,
        "signature": " ".join("%s=%s" % (b, sig[b]) for b in sorted(sig)),
        "legs": legs, "ledger": None,
    }
    if entry is not None:
        rec["ledger"] = {"class": entry.get("class"),
                         "culprit": entry.get("culprit"),
                         "status": status,
                         "diagnosis": entry.get("diagnosis", "")}
    return rec


def digest16(b):
    return hashlib.sha256(b).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

VERDICT_MARK = {
    "exact": ".", "normalized": "~", "panic-equal": "p", "halt-gap": "h",
    "divergence": "X", "error": "E", "unpaired": "u", "skipped": "-",
}

GREEN = {"exact", "normalized", "panic-equal", "halt-gap"}


def print_report(records, skips, backends, elapsed, out=sys.stdout):
    w = out.write
    w("=====================================================================\n")
    w("  hls-xdiff — the cross-impl differential table (Stage 133)\n")
    w("  backends: %s%s\n" % (
        " ".join(backends),
        "  (llvm: static — validated, not executed)" if "llvm" in backends else ""))
    w("=====================================================================\n")
    counts = {}
    for r in records:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        mark = VERDICT_MARK.get(r["verdict"], "?")
        ledger_note = ""
        if r.get("ledger"):
            ledger_note = " [%s: %s]" % (r["ledger"]["status"],
                                         r["ledger"]["class"])
        w("  %s %-42s %-11s %s%s\n" % (
            mark, r["name"][:42], r["verdict"],
            (r["detail"] or "")[:110], ledger_note))
    for name, reason in skips:
        w("  - %-42s %-11s %s\n" % (name[:42], "excluded", reason))
    w("---------------------------------------------------------------------\n")
    order = ["exact", "normalized", "panic-equal", "halt-gap",
             "divergence", "error", "unpaired", "skipped"]
    w("  %d programs in %0.1fs — " % (len(records), elapsed))
    w("  ".join("%s %d" % (v, counts[v]) for v in order if counts.get(v)))
    w("\n")


def records_to_json(records, skips, backends, elapsed, ledger_path):
    out = {
        "schema": 1,
        "tool": "hls-xdiff (Stage 133, v0.151.0-alpha)",
        "backends": list(backends),
        "llvm_is_static": "llvm" in backends,
        "elapsed_s": round(elapsed, 1),
        "ledger": os.path.relpath(ledger_path, REPO),
        "summary": {},
        "excluded": [{"name": n, "reason": r} for n, r in skips],
        "programs": [],
    }
    for r in records:
        legs = {}
        for b, l in r["legs"].items():
            e = {"status": l.get("status")}
            if "exit" in l:
                e["exit"] = l["exit"]
            if "out" in l:
                e["out_sha256_16"] = digest16(l["out"])
                e["out_bytes"] = len(l["out"])
                e["out_canonical_sha256_16"] = digest16(
                    canonical_channel(l["out"].decode("utf-8", "replace")
                                      )[0].encode("utf-8"))
            if l.get("status") == "halted":
                e["trap"] = bool(l.get("trap"))
            for k in ("why", "kind", "notes"):
                if l.get(k):
                    e[k] = l[k]
            legs[b] = e
        out["programs"].append({
            "name": r["name"], "path": r["path"],
            "verdict": r["verdict"], "detail": r["detail"],
            "signature": r["signature"], "legs": legs,
            "ledger": r["ledger"],
        })
    counts = {}
    for r in records:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    out["summary"] = counts
    return out


# ---------------------------------------------------------------------------
# selftest — the pure logic, no backends
# ---------------------------------------------------------------------------

def _ok(out):
    return {"status": "ok", "exit": 0, "out": out.encode()}


def _halt(code):
    return {"status": "halted", "exit": code, "out": b"", "trap": False}


def _trap():
    return {"status": "halted", "exit": 1, "out": b"", "trap": True}


def _skip(why):
    return {"status": "skip", "why": why}


SELFTEST_NORMALIZER = [
    # (left, right, expect_equal) — the pairs the first sweep met
    ("0.750000", "0.75", True),
    ("1.500000", "1.5", True),
    ("3.140000", "3.14", True),
    ("100.000000", "100.0", True),
    ("0.123457", "0.12345678901", True),      # printf rounds; JS does not
    ("123456789012345680.000000", "123456789012345680.0", True),
    ("0.000000", "1e-7", True),               # below the 6dp channel
    ("0.000010", "1e-5", True),
    ("1000000000000000000000.000000", "1e+21", True),
    ("-0.000000", "0.0", True),               # the sign of zero is noise
    ("nan", "NaN", True),
    ("-nan", "nan", True),
    ("inf", "Infinity", True),
    ("-inf", "-Infinity", True),
    # things the canonicalizer must NOT launder
    ("1.10", "1.1", False),                   # version strings keep digits
    ("9007199254740993", "9007199254740994", False),
    ("0.750001", "0.75", False),              # a real 6dp difference
    ("abc", "abd", False),
    ("v1.2.3", "v1.2.4", False),
]

SELFTEST_VERDICTS = [
    # (legs, expect_verdict)
    ({"interp": _ok("a\n"), "native": _ok("a\n"), "wasm": _ok("a\n")}, "exact"),
    ({"interp": _ok("x=0.750000\n"), "wasm": _ok("x=0.75\n")}, "normalized"),
    ({"interp": _halt(101), "native": _halt(101), "wasm": _trap()}, "panic-equal"),
    ({"interp": _halt(7), "native": _halt(7), "wasm": _trap()}, "halt-gap"),
    ({"interp": _ok("a\n"), "wasm": _trap()}, "divergence"),
    ({"interp": _halt(101), "native": _halt(101), "wasm": _ok("unreachable\n")},
     "divergence"),
    ({"interp": _ok("a\n"), "native": _ok("b\n")}, "divergence"),
    ({"interp": _halt(101), "native": _halt(101), "wasm": _halt(3)}, "divergence"),
    ({"interp": _ok("a\n")}, "unpaired"),
    ({"wasm": _skip("subset")}, "skipped"),
]


def selftest(out=sys.stdout):
    fails = 0
    for a, b, expect in SELFTEST_NORMALIZER:
        ca, _ = canonical_channel(a)
        cb, _ = canonical_channel(b)
        got = ca == cb
        if got != expect:
            fails += 1
            out.write("  [FAIL] channel(%r)=%r vs channel(%r)=%r — expected %s\n"
                      % (a, ca, b, cb, "equal" if expect else "unequal"))
    for legs, expect in SELFTEST_VERDICTS:
        verdict, _ = compare_program(legs)
        if verdict != expect:
            fails += 1
            out.write("  [FAIL] verdict(%s) = %s, expected %s\n"
                      % ({k: v["status"] for k, v in legs.items()},
                         verdict, expect))
    # the ledger law: sanctioned / stale / mismatch
    ledger = {"waivers": [
        {"file": "prog_a", "class": "wasm-invalid-module", "culprit": "wasm",
         "states": {"interp": "ok", "native": "ok",
                    "wasm": "error(invalid-module)"}},
        {"file": "prog_b", "class": "wasm-missing-check", "culprit": "wasm",
         "states": {"interp": "halt(101)", "native": "halt(101)",
                    "wasm": "ok"}},
    ]}
    legs_div = {"interp": _ok("a"), "native": _ok("a"),
                "wasm": {"status": "error", "kind": "invalid-module"}}
    st, _ = ledger_judge("prog_a", "divergence", "", legs_div, ledger)
    if st != "sanctioned":
        fails += 1
        out.write("  [FAIL] ledger: named divergence should be sanctioned, got %s\n" % st)
    legs_clean = {"interp": _ok("a"), "native": _ok("a"), "wasm": _ok("a")}
    st, _ = ledger_judge("prog_a", "exact", "", legs_clean, ledger)
    if st != "stale":
        fails += 1
        out.write("  [FAIL] ledger: clean program should make its entry stale, got %s\n" % st)
    legs_moved = {"interp": _ok("a"), "native": _halt(3),
                  "wasm": {"status": "error", "kind": "invalid-module"}}
    st, _ = ledger_judge("prog_a", "divergence", "", legs_moved, ledger)
    if st != "mismatch":
        fails += 1
        out.write("  [FAIL] ledger: a moved divergence should mismatch, got %s\n" % st)
    legs_haltnow = {"interp": _halt(101), "native": _halt(101), "wasm": _ok("")}
    st, _ = ledger_judge("prog_b", "divergence", "", legs_haltnow, ledger)
    if st != "sanctioned":
        fails += 1
        out.write("  [FAIL] ledger: missing-check entry should sanction, got %s\n" % st)
    st, _ = ledger_judge("prog_b", "exact", "", legs_clean, ledger)
    if st != "stale":
        fails += 1
        out.write("  [FAIL] ledger: healed program should be stale, got %s\n" % st)
    if fails == 0:
        out.write("selftest: %d normalizer + %d verdict + 5 ledger vectors green\n"
                  % (len(SELFTEST_NORMALIZER), len(SELFTEST_VERDICTS)))
        return 0
    out.write("selftest: %d FAILURES\n" % fails)
    return 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Stage 133: the cross-implementation differential suite.")
    ap.add_argument("--file", metavar="PROG", help="one program, every leg")
    ap.add_argument("--core", action="store_true",
                    help="the curated core corpus (fast)")
    ap.add_argument("--all", action="store_true",
                    help="lift the purity gate (impure programs earn what they earn)")
    ap.add_argument("--backends", default="interp,native,wasm,llvm",
                    help="comma list from interp,native,wasm,llvm")
    ap.add_argument("--timeout", type=int, default=60, metavar="S",
                    help="per-leg seconds (default 60)")
    ap.add_argument("--jobs", type=int, default=2, metavar="N")
    ap.add_argument("--json", metavar="PATH", help="write the machine report")
    ap.add_argument("--ledger", metavar="PATH", default=LEDGER_PATH,
                    help="override the waiver ledger path")
    ap.add_argument("--discover", action="store_true",
                    help="the purity census only — no legs run")
    ap.add_argument("--list", action="store_true", help="print the corpus")
    ap.add_argument("--strict", action="store_true",
                    help="demote normalized/halt-gap to failures")
    ap.add_argument("--update-ledger", action="store_true",
                    help="draft waiver entries for the diverging set")
    ap.add_argument("selftest", nargs="?", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.selftest == "selftest":
        return selftest()
    if (args.selftest or "").startswith("self"):
        ap.error("did you mean: hls-xdiff selftest")

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    for b in backends:
        if b not in LEGS:
            sys.stderr.write("hls-xdiff: unknown backend '%s' (choose from %s)\n"
                             % (b, ",".join(LEGS)))
            return 2
    if "native" in backends and "interp" not in backends:
        sys.stderr.write("hls-xdiff: the interp leg is the reference — "
                         "it cannot be dropped while native runs\n")
        return 2

    if args.file:
        path = args.file if os.path.isabs(args.file) else \
            os.path.join(REPO, args.file)
        if not os.path.exists(path):
            sys.stderr.write("hls-xdiff: no such program: %s\n" % args.file)
            return 2
        corpus, skips = [(os.path.basename(path)[:-4], path)], []
    elif args.core:
        corpus, skips = resolve_core(), []
    else:
        corpus, skips = discover_corpus(gated=not args.all)

    if args.discover:
        print("purity census: %d eligible, %d excluded" % (len(corpus), len(skips)))
        for name, reason in skips:
            print("  excluded %-44s %s" % (name, reason))
        return 0
    if args.list:
        for name, path in corpus:
            print(name)
        return 0
    if not corpus:
        sys.stderr.write("hls-xdiff: empty corpus\n")
        return 2

    ledger, err = load_ledger(args.ledger)
    if err:
        sys.stderr.write("hls-xdiff: %s\n" % err)
        return 2

    import time
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        records = list(pool.map(lambda item: run_program(
            item, backends, args.timeout, ledger, strict=args.strict), corpus))
    elapsed = time.time() - t0
    records.sort(key=lambda r: r["name"])

    print_report(records, skips, backends, elapsed)

    if args.update_ledger:
        drafts = []
        for r in records:
            if r["verdict"] in ("divergence", "error") and not r.get("ledger"):
                drafts.append({
                    "file": r["name"], "class": "TODO", "culprit": "TODO",
                    "states": dict(
                        (b, leg_state(l)) for b, l in r["legs"].items()
                        if l.get("status") in ("ok", "halted", "error")),
                    "diagnosis": r["detail"],
                })
        print("\n--update-ledger: %d draft entries (review, classify, splice "
              "into %s by hand — the tool never writes the ledger):" %
              (len(drafts), os.path.relpath(args.ledger, REPO)))
        print(json.dumps(drafts, indent=2))

    if args.json:
        report = records_to_json(records, skips, backends, elapsed, args.ledger)
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)

    # the verdict law: ledger discipline first (a stale entry is a
    # failure even when the program itself compares clean), then the
    # verdict classes
    exec_backends = [b for b in backends if b in EXECUTING_BACKENDS]
    failures = []
    for r in records:
        v = r["verdict"]
        led = r.get("ledger")
        if led and led["status"] in ("stale", "mismatch"):
            failures.append((r["name"], "ledger-" + led["status"],
                             "%s (ledger class %s)" % (r["detail"], led["class"])))
            continue
        if v in GREEN and not args.strict:
            continue
        if v == "exact":
            continue
        if v in ("normalized", "halt-gap") and args.strict:
            failures.append((r["name"], v, r["detail"]))
            continue
        if led and led["status"] == "sanctioned":
            continue
        if v in ("divergence", "error"):
            failures.append((r["name"], v, r["detail"]))
    exec_count = sum(1 for b in exec_backends if any(
        r["legs"].get(b, {}).get("status") in ("ok", "halted")
        for r in records))
    if exec_count < 2 and exec_backends:
        failures.append(("(suite)", "no-differential-pair",
                         "only %d executing backend(s) ran; the suite needs 2+ "
                         "to compare anything" % exec_count))
    if failures:
        print("NOT GREEN: %d failure(s):" % len(failures))
        for name, kind, detail in failures:
            print("  %s %s: %s" % (kind, name, detail))
        return 1
    print("GREEN: every executing pair agreed (or its divergence is "
          "ledgered); the ledger is consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
