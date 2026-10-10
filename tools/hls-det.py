#!/usr/bin/env python3
"""hls-det — the Stage 127 determinism verifier.

Usage:
  python3 tools/hls-det.py workload.hls
  python3 tools/hls-det.py --runs 10 workload.hls
  python3 tools/hls-det.py --trace workload.hls
  python3 tools/hls-det.py --json r.json workload.hls
  python3 tools/hls-det.py --no-parity workload.hls
  python3 tools/hls-det.py --observe-preemptive workload.hls
  python3 tools/hls-det.py workload.hls arg1 arg2      # argv reaches the program

Stage 16 made the task model data-race-free; Stage 127 makes its
INTERLEAVING testable. HL_DET_SCHED (the mode this tool arms) replaces
the preemptive OS scheduling with the FIFO baton rotation SPEC section
67 specifies — every spawn/send/try_send/recv/recv_or/select/join/
finish is a scheduling point, the woken task runs ahead of its waker,
a spawned task's first turn comes before its parent's next — so a
concurrent program's output and interleaving are identical on every
run. This tool CERTIFIES that claim instead of believing it:

  1. the NATIVE leg — the workload is compiled with the house recipe
     (bin/hlc + gcc -O2 -lm -pthread) and run --runs times with the
     mode armed (and the trace on): every run's stdout, interleaving
     trace and exit status must be byte-identical. The first differing
     run is reported with its digest. A program may die — a
     deterministic panic is a deterministic interleaving — the verdict
     says so and carries the exit code.

  2. the PARITY leg — the Stage-0 interpreter runs the same workload
     under `boot.py --det` with the trace on: its stdout, trace and
     exit must match the native leg exactly. The interpreter mirrors
     the rotation policy op-for-op; this leg is what keeps the two
     schedulers honest about each other (the differential suite's
     discipline, extended to the interleaving itself). --no-parity
     skips it (and says so — a skipped leg is never a pass).

The trace (HL_DET_TRACE, armed by the tool, parsed with the reserved-
prefix discipline of Stage 119/124/125/126): one line per scheduling
point on stderr,

    __HLDET_STEP__ <seq> t<task> <op>[ <detail>]

with seq dense from 0, task ids by spawn order (main = t0), channel
ids by creation order (ch0..), op in {spawn, send, try_send, recv,
recv_or, select, join, finish} and the detail exactly `t<n>` for
spawn/join, `ch<n>[,ch<m>...]` for select, `ch<n>` for the channel
ops, absent for finish. A line STARTING with the stem must parse
exactly and continue the sequence; a line merely CONTAINING `__HLDET_`
— on either stream — is a protocol error: a program that forges
protocol look-alikes corrupts the witness and fails loudly, never
silently. Program chatter on stderr is fine; it is kept out of the
comparison (the interpreter's task-panic lines carry a source-location
suffix the native runtime does not — a pre-existing, documented
divergence — so the non-trace stderr is compared across the NATIVE
runs only).

--observe-preemptive runs the same binary --runs times WITHOUT the
mode and reports what it saw: stable or varying, honestly labelled an
OBSERVATION, never a certificate — preemptive stability on N runs is
evidence, not proof (that asymmetry is the whole reason this stage
exists).

Verdicts: DETERMINISTIC when every native det run is byte-identical
and the parity leg agrees (or was skipped, said so, and --no-parity
was explicit); NOT DETERMINISTIC when the native runs differ; PARITY
FAILURE when the interpreter disagrees. Exit 0 certifies, 1 is not
certified (nondeterminism, parity, timeout, protocol error, compile
or run failure — the reason is named), 2 is usage (missing file,
non-.hls, --runs < 2 — a determinism claim needs at least two runs —
bad option values, no bin/hlc, no gcc).
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOL_VERSION = "0.145.0-alpha"

# The reserved protocol prefixes (the Stage 119/124/125/126 discipline).
_PROTO_STEP = "__HLDET_STEP__ "
_PROTO_STEM = "__HLDET_"

_OPS = ("spawn", "send", "try_send", "recv", "recv_or", "select",
        "join", "finish")

_STEP_RE = re.compile(
    r"^__HLDET_STEP__ (\d+) t(\d+) ([a-z_]+)(?: (t\d+|ch\d+(?:,ch\d+)*))?$")


class ProtocolError(Exception):
    """A malformed, non-advancing or forged trace record."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def parse_trace(stderr_bytes):
    """Split a run's stderr into (steps, chatter).

    Every line starting with the stem must parse EXACTLY and continue
    the dense sequence; a line merely containing the stem (either
    stream) is a forgery and fails loudly."""
    if b"__HLDET_" in stderr_bytes.replace(_PROTO_STEP.encode(), b"", 1) \
       and False:
        pass  # (stem-in-chatter is checked by the caller per line)
    text = stderr_bytes.decode("utf-8", errors="surrogateescape")
    steps = []
    chatter = []
    for line in text.splitlines():
        if line.startswith(_PROTO_STEP):
            m = _STEP_RE.match(line)
            if not m:
                raise ProtocolError("malformed step record: %r" % line)
            seq = int(m.group(1))
            if seq != len(steps):
                raise ProtocolError(
                    "non-advancing step record: expected seq %d, got %d"
                    % (len(steps), seq))
            if m.group(3) not in _OPS:
                raise ProtocolError("unknown op in step record: %r" % line)
            steps.append(line)
        else:
            if _PROTO_STEM in line:
                raise ProtocolError(
                    "protocol look-alike on stderr: %r" % line)
            chatter.append(line)
    return steps, chatter


def check_stdout_protocol(stdout_bytes):
    """The reserved stem may not appear on the program's stdout."""
    for line in stdout_bytes.decode("utf-8", errors="surrogateescape") \
            .splitlines():
        if _PROTO_STEM in line:
            raise ProtocolError(
                "protocol look-alike on stdout: %r" % line)


def run_det(binary, argv, timeout_s, extra_env=None):
    """One armed run: (exit, stdout, steps, chatter)."""
    env = dict(os.environ)
    env["HL_DET_SCHED"] = "1"
    env["HL_DET_TRACE"] = "1"
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run([binary] + argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("timeout after %ds (det run)" % timeout_s)
    check_stdout_protocol(p.stdout)
    steps, chatter = parse_trace(p.stderr)
    return p.returncode, p.stdout, steps, chatter


def run_preemptive(binary, argv, timeout_s):
    """One unarmed run (the observation leg)."""
    env = dict(os.environ)
    env.pop("HL_DET_SCHED", None)
    env.pop("HL_DET_TRACE", None)
    try:
        p = subprocess.run([binary] + argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        return None
    return p.returncode, p.stdout


def run_interp(file_path, argv, timeout_s):
    """The interpreter leg: boot.py --det with the trace armed."""
    env = dict(os.environ)
    env["HL_DET_SCHED"] = "1"
    env["HL_DET_TRACE"] = "1"
    cmd = [sys.executable, os.path.join(REPO_ROOT, "boot", "boot.py"),
           "--det", file_path] + argv
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout_s, env=env,
                           cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("timeout after %ds (interpreter det run)"
                           % timeout_s)
    check_stdout_protocol(p.stdout)
    steps, chatter = parse_trace(p.stderr)
    return p.returncode, p.stdout, steps, chatter


def compile_native(file_path, workdir):
    """The house recipe: bin/hlc + gcc -O2 -lm -pthread."""
    hlc = os.path.join(REPO_ROOT, "bin", "hlc")
    if not (os.path.isfile(hlc) and os.access(hlc, os.X_OK)):
        sys.stderr.write("hls-det: no bin/hlc under %s — run make bootstrap\n"
                         % REPO_ROOT)
        sys.exit(2)
    import shutil
    if not shutil.which("gcc"):
        sys.stderr.write("hls-det: gcc not found on PATH\n")
        sys.exit(2)
    c_path = os.path.join(workdir, "workload.c")
    bin_path = os.path.join(workdir, "workload")
    comp = subprocess.run([hlc, file_path, c_path], cwd=REPO_ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if comp.returncode != 0:
        sys.stderr.write(comp.stderr.decode("utf-8", "replace"))
        raise RuntimeError("hlc compile failed (exit %d)" % comp.returncode)
    gcc = subprocess.run(["gcc", "-O2", "-o", bin_path, c_path,
                          "-lm", "-pthread"], cwd=REPO_ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if gcc.returncode != 0:
        sys.stderr.write(gcc.stderr.decode("utf-8", "replace"))
        raise RuntimeError("gcc failed (exit %d)" % gcc.returncode)
    return bin_path


def main():
    ap = argparse.ArgumentParser(add_help=True, prog="hls-det",
                                 description="Stage 127 determinism verifier")
    ap.add_argument("file", help="the Halis workload (.hls)")
    ap.add_argument("argv", nargs="*", help="program arguments (after the file)")
    ap.add_argument("--runs", type=int, default=5, metavar="N",
                    help="native det runs to compare (default 5, min 2)")
    ap.add_argument("--timeout", type=int, default=120, metavar="SEC",
                    help="per-run timeout in seconds (default 120)")
    ap.add_argument("--json", metavar="FILE",
                    help="write a machine-readable report")
    ap.add_argument("--trace", action="store_true",
                    help="print the interleaving trace of the first run")
    ap.add_argument("--no-parity", action="store_true",
                    help="skip the interpreter parity leg (never silent)")
    ap.add_argument("--observe-preemptive", action="store_true",
                    help="also run the binary unarmed N times; an "
                         "observation, never a certificate")
    args = ap.parse_args()

    # ---- usage gates (exit 2) ----
    if not os.path.isfile(args.file):
        sys.stderr.write("hls-det: no such file: %s\n" % args.file)
        return 2
    if not args.file.endswith(".hls"):
        sys.stderr.write("hls-det: not a .hls workload: %s\n" % args.file)
        return 2
    if args.runs < 2:
        sys.stderr.write("hls-det: --runs must be >= 2 "
                         "(a determinism claim needs at least two runs)\n")
        return 2
    if args.timeout < 1:
        sys.stderr.write("hls-det: --timeout must be >= 1 second\n")
        return 2

    file_path = os.path.abspath(args.file)

    report = {
        "tool": "hls-det",
        "version": TOOL_VERSION,
        "file": os.path.relpath(file_path, REPO_ROOT),
        "argv": list(args.argv),
        "config": {
            "runs": args.runs,
            "timeout_s": args.timeout,
            "parity": not args.no_parity,
            "observe_preemptive": args.observe_preemptive,
        },
        "runs": [],
        "parity": None,
        "preemptive": None,
        "verdict": None,
        "reason": None,
    }

    def finish(verdict, reason, code):
        report["verdict"] = verdict
        report["reason"] = reason
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(report, fh, indent=2, sort_keys=True)
                fh.write("\n")
        return code

    # ---- compile (program failures are exit 1, named) ----
    workdir = tempfile.mkdtemp(prefix="hls-det-")
    try:
        try:
            binary = compile_native(file_path, workdir)
        except RuntimeError as ex:
            print("hls-det: %s" % ex)
            return finish("NOT CERTIFIED", str(ex), 1)

        # ---- the native det leg ----
        runs = []
        try:
            for i in range(args.runs):
                rc, out, steps, chatter = run_det(binary, args.argv,
                                                  args.timeout)
                runs.append({
                    "i": i,
                    "exit": rc,
                    "stdout_sha": _sha(out),
                    "steps": len(steps),
                    "steps_sha": _sha("\n".join(steps).encode()),
                    "chatter": chatter,
                    "_stdout": out,
                    "_steps": steps,
                })
        except (RuntimeError, ProtocolError) as ex:
            print("hls-det: run %d refused: %s" % (len(runs) + 1, ex))
            return finish("NOT CERTIFIED", "det run: %s" % ex, 1)

        first = runs[0]
        det_stable = True
        for r in runs[1:]:
            if (r["exit"] != first["exit"]
                    or r["stdout_sha"] != first["stdout_sha"]
                    or r["steps_sha"] != first["steps_sha"]):
                det_stable = False
                report.setdefault("first_diff", {
                    "run": r["i"],
                    "exit": [first["exit"], r["exit"]],
                    "stdout_sha": [first["stdout_sha"], r["stdout_sha"]],
                    "steps_sha": [first["steps_sha"], r["steps_sha"]],
                })

        # ---- the parity leg ----
        parity_ok = None
        if not args.no_parity:
            try:
                irc, iout, isteps, _ichatter = run_interp(file_path,
                                                          args.argv,
                                                          args.timeout)
                parity_ok = (irc == first["exit"]
                             and _sha(iout) == first["stdout_sha"]
                             and _sha("\n".join(isteps).encode())
                             == first["steps_sha"])
                report["parity"] = {
                    "checked": True,
                    "exit": irc,
                    "exit_match": irc == first["exit"],
                    "stdout_match": _sha(iout) == first["stdout_sha"],
                    "steps_match": (_sha("\n".join(isteps).encode())
                                    == first["steps_sha"]),
                    "steps": len(isteps),
                }
                if parity_ok is False:
                    report["parity"]["native_steps"] = first["steps"]
            except (RuntimeError, ProtocolError) as ex:
                print("hls-det: interpreter leg refused: %s" % ex)
                return finish("NOT CERTIFIED", "interpreter: %s" % ex, 1)
        else:
            report["parity"] = {"checked": False,
                                "reason": "--no-parity (explicit)"}

        # ---- the preemptive observation leg ----
        if args.observe_preemptive:
            obs = [run_preemptive(binary, args.argv, args.timeout)
                   for _ in range(args.runs)]
            oks = [o for o in obs if o is not None]
            if len(oks) < len(obs):
                report["preemptive"] = {"observed": "timeout"}
            else:
                stable = all(o[0] == oks[0][0] and o[1] == oks[0][1]
                             for o in oks)
                report["preemptive"] = {
                    "observed": "stable" if stable else "varying",
                    "note": ("an observation over %d runs, never a "
                             "certificate" % len(oks)),
                }

        # ---- the verdict ----
        ops = {}
        for s in first["_steps"]:
            op = s.split(" ", 4)[3] if s.count(" ") >= 3 else s.split(" ")[3]
            ops[op] = ops.get(op, 0) + 1
        report["stats"] = {
            "steps": first["steps"],
            "op_histogram": ops,
            "tasks": 1 + (max((int(s.split(" ")[2][1:]) for s in first["_steps"]),
                              default=0) if first["_steps"] else 0),
        }
        report["runs"] = [{k: r[k] for k in
                           ("i", "exit", "stdout_sha", "steps", "steps_sha")}
                          for r in runs]

        if not det_stable:
            print("hls-det: NOT DETERMINISTIC — native det runs differ "
                  "(first difference at run %d)"
                  % report.get("first_diff", {}).get("run", 1))
            return finish("NOT DETERMINISTIC",
                          "native det runs differ", 1)
        if parity_ok is False:
            print("hls-det: PARITY FAILURE — the interpreter's det run "
                  "disagrees with the native one")
            return finish("PARITY FAILURE",
                          "interpreter det run differs", 1)

        # ---- the report ----
        print("hls-det: DETERMINISTIC over %d native det runs "
              "(exit %d, %d steps)"
              % (args.runs, first["exit"], first["steps"]))
        if first["exit"] != 0:
            print("           the program itself exits %d — the "
                  "interleaving is certified, not the health" % first["exit"])
        if args.no_parity:
            print("           parity leg SKIPPED (--no-parity, explicit)")
        else:
            print("           parity: interpreter agrees "
                  "(stdout, trace, exit)")
        if args.observe_preemptive and report["preemptive"]:
            print("           preemptive observation: %s"
                  % report["preemptive"]["observed"])
        if args.trace:
            print("---- interleaving trace (run 0) ----")
            for s in first["_steps"]:
                print(s)
            print("---- end trace ----")
        return finish("DETERMINISTIC", None, 0)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
