#!/usr/bin/env python3
"""hls-size — the Stage 139 per-commit binary-size regression
dashboard.

Usage:
  python3 tools/hls-size.py record    [--suite DIR] [--instrument S]
                                      [--grep S] [--from-report R.json]
                                      [--commit SHA] [--note TEXT]
                                      [--allow-dirty]
                                      [--history PATH] [--json OUT]
  python3 tools/hls-size.py sanction WORKLOAD --reason TEXT
                                      [--commit SHA] [--author NAME]
                                      [--history PATH]
  python3 tools/hls-size.py gate      [--baseline SHA]
                                      [--history PATH] [--json OUT]
                                      [--quiet]
  python3 tools/hls-size.py dashboard --out FILE.md [--limit N]
                                      [--history PATH] [--json OUT]
                                      [--stamp]
  python3 tools/hls-size.py history   [--workload NAME] [--json]
                                      [--history PATH]

Roadmap Stage 139 is one phrase: "Binary-size regression dashboard
(per-commit)". Stage 136 gave RUNTIME its ledger (hls-perf), Stage 137
gave MEMORY its own (hls-mem), Stage 138 gave the COMPILER's wall time
its own (hls-ctime) — this stage gives the BYTES their ledger, the
axis a language release can regress that no timing sees: the tool
gets fatter to ship, and every program it ships gets fatter with it.

Binary size is the axis with NO NOISE. The emitted C is byte-exact by
the bootstrap invariant, the native link recipe is fixed, the same
toolchain on the same architecture always produces the same bytes —
so hls-size keeps NO samples, runs NO statistics, and hides behind
NO noise band: one byte of growth is a regression the gate must
name. What Stage 124's Welch law is to hls-perf and hls-ctime, the
integer byte compare is to hls-size — and it is the whole law:

    cur > base  ->  regressed     (needs a sanction)
    cur == base ->  no-change
    cur < base  ->  improved      (free, always)

Every workload of the tracked suite (benchmarks/size_watch/ — five
deterministic, size-shaped workloads: the runtime floor every binary
stands on, the string surface, the per-function call machinery, the
type-driven struct emission, the match dispatch) is measured on FIVE
AXES:

  c_bytes    bytes of the emitted C — the emitter's girth
  bin_bytes  bytes of the linked native binary — what the user ships
  text_bytes code section  \\ the `size(1)` breakdown of bin_bytes —
  data_bytes data section   \\ where the growth actually landed, at
  bss_bytes  zero-init state // a resolution a file size cannot give

A workload's class is the WORST of its axes: one axis growing
regresses the workload, every axis shrinking-or-equal with at least
one strict shrink improves it, everything else is no-change.

The instrument is the compiler, and Halis ships two of them, so they
are two INSTRUMENTS, never compared:

  stage0  python3 tools/hlinterp/boot.py src/hlc.hls <workload> <out.c>
  native  bin/hlc <workload> <out.c>

For the emitted bytes both instruments must agree byte-for-byte (the
bootstrap invariant is the tool's own cross-check), but the records
stay separate — and the native record carries one more workload the
stage0 record cannot: `hlc_self`, the compiler's OWN girth (the size
of bin/hlc.c, bin/hlc and its sections), the row that answers "how
fat is the tool this commit".

A record also pins the MACHINE the bytes were made on: the cc
version line, the architecture and the exact link recipe. The gate
refuses to compare records across different machines — sizes from
two toolchains are two different languages of the same program.

hls-size is a LEDGER over commits:

  1. a RECORD is one measurement pinned to one commit. `record`
     compiles the tracked suite through the chosen instrument,
     links each emission with the Makefile's own recipe (cc -O2
     -lm -pthread), reads the five axes, and appends an entry keyed
     to `git rev-parse HEAD` — refused when the work tree carries
     tracked modifications, because a per-commit record that does
     not pin a clean commit is a rumour, not a measurement. Every
     record pins the compiler's OWN identity — the sha256 of the
     compiler source and of the compiler BINARY — and the
     STALE-BINARY LAW bites at record time: a native record whose
     binary digest was already filed against a DIFFERENT source
     digest is refused ("run make bootstrap first"). `--from-report`
     imports a machine report instead of measuring (the CI path);

  2. a SANCTION is a written excuse: one workload, one reason, one
     author. `sanction hlc_self --reason "the checker grew a second
     pass; the binary paid for it"` — a regression with a sanction
     is reported, not failed. A sanction without a reason is
     refused (an excuse nobody wrote down is not an excuse);

  3. the GATE is the law. `gate` classifies every workload of the
     newest record against the prior record of the SAME INSTRUMENT
     (byte-identical suite, same machine fingerprint): no-change /
     improved / regressed, plus new and vanished workloads on
     either side of a rename. One unsanctioned regressed or
     vanished workload exits 1 naming it; sanctioned rows are loud
     but green;

  4. the DASHBOARD renders the ledger — BINARYSIZE.md with the
     verdict table (every axis, base to now), per-workload bin_bytes
     trend sparklines across the recorded commits, and the
     sanctions register. It is DETERMINISTIC: sorted walks, no
     wall-clock unless --stamp (and then only the Generated line),
     the sha256 ledger digest embedded — two renders of one ledger
     are byte-identical, so a hand-edited dashboard is a diff.

The history is a JSONL file (default .hlsize/history.jsonl,
gitignored — pass --history to point CI at a committed ledger).
Append-only by construction: `record` and `sanction` append, nothing
rewrites, and a line that does not parse is a named error, never a
silent skip.

Exit codes:
  0  verified (gate pass, dashboard rendered, record filed)
  1  named violation (gate failure, dirty tree, corrupt line,
     instrument mismatch, machine mismatch, stale compiler binary)
  2  usage / IO error (bad paths, empty ledger, unknown workload,
     a sanction without a reason, no compiler to measure with)

Layering note: hls-bench (Stage 124) measures programs; hls-perf
(Stage 136) remembers program time; hls-mem (Stage 137) remembers
program memory; hls-ctime (Stage 138) remembers the time of the
COMPILER itself; hls-size (this tool) remembers the BYTES of the
compiler's own output — the shipped girth, program and tool both.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time

TOOL_VERSION = "0.157.0"
TOOL = "hls-size"
HISTORY_DEFAULT = os.path.join(".hlsize", "history.jsonl")
DEFAULT_SUITE = os.path.join("benchmarks", "size_watch")
DEFAULT_DASH_LIMIT = 12
INSTRUMENTS = ("stage0", "native")
SELF_WORKLOAD = "hlc_self"

# The five axes every workload is measured on. The order is the order
# the dashboard prints them; the worst-axis law walks them all.
AXES = ("c_bytes", "bin_bytes", "text_bytes", "data_bytes", "bss_bytes")
# The axes the gate compares a machine on — cc, arch and recipe.
ENV_LAW_KEYS = ("cc", "arch", "recipe")

# The link recipe is the Makefile's own (make bootstrap step 2, the
# run target) — the dashboard measures what `make run` would ship,
# never a bespoke build a release would not.
CC_DEFAULT = "gcc"
CC_FLAGS = ["-O2"]
CC_LIBS = ["-lm", "-pthread"]
COMPILE_RECIPE = "%s %s -o out out.c %s" % (
    CC_DEFAULT, " ".join(CC_FLAGS), " ".join(CC_LIBS))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HLC_SOURCE_ENTRY = os.path.join("src", "hlc.hls")
HLC_SOURCE_DIR = os.path.join("src", "hlc")
BOOT_DRIVER = os.path.join("tools", "hlinterp", "boot.py")
NATIVE_COMPILER = os.path.join("bin", "hlc")
SCRATCH = os.path.join(".hlsize-out")

_SPARK_BANDS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_SPARK_MID = "\u2584"


class HlSizeError(Exception):
    """A named refusal. .exit is the process exit code it deserves."""

    def __init__(self, msg, exit=1):
        super().__init__(msg)
        self.exit = exit


# ---------------------------------------------------------------------------
# Small helpers — bytes, digests, json, the git surface.
# ---------------------------------------------------------------------------

def json_dumps_sorted(obj):
    return json.dumps(obj, sort_keys=True, indent=1) + "\n"


def json_line(obj):
    """One ledger line. COMPACT — the history is JSONL, one self-
    contained object per line; pretty-printing here would shatter an
    entry across lines and corrupt every later parse."""
    return json.dumps(obj, sort_keys=True) + "\n"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def is_sha1(s):
    return (len(s) == 40
            and all(c in "0123456789abcdef" for c in s))


def is_sha256(s):
    return (len(s) == 64
            and all(c in "0123456789abcdef" for c in s))


def short(s, n=10):
    return s[:n]


def fmt_bytes(n):
    """A byte count fit for a table cell — KiB/MiB when the number
    is round enough, exact digits otherwise (the gate's law is the
    exact integer; the cell is for the human reading it)."""
    if n is None:
        return "—"
    if n >= 1048576 and n % 1024 == 0:
        return "%dMiB" % (n // 1048576)
    if n >= 1024 and n % 1024 == 0:
        return "%dKiB" % (n // 1024)
    return str(n)


def fmt_delta(d):
    """A signed byte delta: +1234 / -1234 / 0."""
    if d is None:
        return "—"
    if d > 0:
        return "+%d" % d
    return str(d)


def fmt_pct(p):
    """A signed percent, one decimal — sparkline-adjacent, cell-fit.
    Matches hls-bench's fmt_pct shape so the dashboards agree."""
    if p is None:
        return "—"
    return "%+.1f%%" % p


def git(args, root):
    """Run git in root; None when git is absent or the tree is not a
    work tree — callers decide whether that is fatal."""
    try:
        p = subprocess.run(["git", "-C", root] + args,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def git_head(root):
    return git(["rev-parse", "HEAD"], root)


def git_user(root):
    name = git(["config", "user.name"], root)
    return name if name else "unknown"


# ---------------------------------------------------------------------------
# The compiler identity — the two digests every record pins.
# ---------------------------------------------------------------------------

def compiler_source_digest(root):
    """sha256 over the sorted (relpath, bytes) walk of the compiler
    source — src/hlc.hls and every src/hlc/*.hls module. This is the
    identity of the CODE WHOSE OUTPUT IS MEASURED: the tracked suite
    changes what the compiler compiles, the source digest says which
    compiler compiled it."""
    d = root
    members = []
    if not os.path.isfile(os.path.join(d, HLC_SOURCE_ENTRY)):
        raise HlSizeError("the compiler entry %s does not exist under %s"
                          % (HLC_SOURCE_ENTRY, root), exit=2)
    members.append((HLC_SOURCE_ENTRY,
                    os.path.join(d, HLC_SOURCE_ENTRY)))
    srcdir = os.path.join(d, HLC_SOURCE_DIR)
    if not os.path.isdir(srcdir):
        raise HlSizeError("the compiler module directory %s does not "
                          "exist under %s" % (HLC_SOURCE_DIR, root), exit=2)
    for fn in sorted(os.listdir(srcdir)):
        if fn.endswith(".hls"):
            rel = os.path.join(HLC_SOURCE_DIR, fn)
            members.append((rel, os.path.join(srcdir, fn)))
    h = hashlib.sha256()
    for rel, path in members:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise HlSizeError("cannot read the compiler source %s: %s"
                              % (rel, exc), exit=2)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(blob)
        h.update(b"\0")
    return h.hexdigest()


def compiler_binary_digest(root, instrument):
    """sha256 of the compiler BINARY actually measured through —
    bin/hlc for the native instrument (an untracked build artifact,
    so the record pins its bytes itself: the clean-tree check cannot
    vouch for it), tools/hlinterp/boot.py for stage0 (the driver the chain
    runs through)."""
    path = os.path.join(root, NATIVE_COMPILER if instrument == "native"
                        else BOOT_DRIVER)
    if not os.path.isfile(path):
        raise HlSizeError(
            "the %s instrument needs %s — run `make bootstrap` first"
            % (instrument, NATIVE_COMPILER if instrument == "native"
               else BOOT_DRIVER), exit=2)
    try:
        with open(path, "rb") as fh:
            return sha256_hex(fh.read())
    except OSError as exc:
        raise HlSizeError("cannot read %s: %s" % (path, exc), exit=2)


# ---------------------------------------------------------------------------
# The suite — the tracked workload directory and its digest.
# ---------------------------------------------------------------------------

def suite_members(root, suite):
    """The tracked workloads: every *.hls file directly in the suite
    directory, sorted by stem — the suite IS the set; the ledger's
    suite digest covers names AND bytes, so a renamed or edited
    workload re-baselines, exactly as an edited bench suite does."""
    d = os.path.join(root, suite)
    if not os.path.isdir(d):
        raise HlSizeError("the suite directory %s does not exist" % suite,
                          exit=2)
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".hls"):
            out.append((fn[:-len(".hls")], os.path.join(d, fn)))
    if not out:
        raise HlSizeError("the suite directory %s carries no .hls "
                          "workloads" % suite, exit=2)
    return out


def members_digest(root, members):
    """sha256 over the sorted (name, bytes) pairs — names included, so
    a rename is a change, not a coincidence."""
    h = hashlib.sha256()
    for name, path in members:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise HlSizeError("cannot read the workload %s: %s"
                              % (path, exc), exit=2)
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(blob)
        h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The instrument — compile one workload, link it, read the five axes.
# ---------------------------------------------------------------------------

def section_sizes(binary, cc=None):
    """text / data / bss through size(1). Berkeley layout (the GNU
    and LLVM default alike): `text data bss dec hex filename` — the
    parse takes the LAST row whose first four fields are integers,
    so a header rename or an extra trailer line cannot poison it.
    A `size` that will not speak it is a named refusal, never zeros
    (silent zeros would make bss look frozen forever)."""
    try:
        p = subprocess.run(["size", binary],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HlSizeError("cannot read the sections of %s: %s "
                          "(this tool needs a working size(1))"
                          % (binary, exc), exit=2)
    if p.returncode != 0:
        raise HlSizeError("size(1) refused %s: %s" % (binary,
                          (p.stderr or "").strip()), exit=2)
    for line in reversed((p.stdout or "").strip().splitlines()):
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            text = int(fields[0])
            data = int(fields[1])
            bss = int(fields[2])
            dec = int(fields[3])
        except ValueError:
            continue
        if text + data + bss == dec:
            return text, data, bss
    raise HlSizeError(
        "size(1) output for %s is not the berkeley layout this tool "
        "parses (text data bss dec hex) — record refused rather than "
        "file sections it cannot read" % binary, exit=2)


def compile_once(instrument, workload, out_c, root):
    """One full HLS-to-C compile of the workload through the chosen
    instrument. A compile that does not exit 0 is a named refusal —
    the suite compiles green or the record does not exist (a
    dashboard over half a suite is a lie). The emitted C is kept: it
    is the artifact a size record measures, not a byproduct."""
    if instrument == "native":
        cmd = [os.path.join(root, NATIVE_COMPILER),
               os.path.abspath(workload), out_c]
    else:
        cmd = [sys.executable,
               os.path.join(root, BOOT_DRIVER),
               os.path.join(root, HLC_SOURCE_ENTRY),
               os.path.abspath(workload), out_c]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True,
                           cwd=root, timeout=600)
    except subprocess.TimeoutExpired:
        raise HlSizeError("the compile of %s timed out after 600s "
                          "(instrument %s)"
                          % (os.path.basename(workload), instrument))
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()
        detail = tail[-1] if tail else "(no output)"
        raise HlSizeError(
            "the compile of %s failed (%s, exit %d): %s — file a "
            "bug, not a record"
            % (os.path.basename(workload), instrument, p.returncode,
               detail))
    if not os.path.isfile(out_c) or os.path.getsize(out_c) == 0:
        raise HlSizeError(
            "the compile of %s produced no C (instrument %s) — file a "
            "bug, not a record"
            % (os.path.basename(workload), instrument))


def measure_workload(instrument, workload, out_dir, root, cc):
    """The real measurement: compile once, link once with the
    Makefile's recipe, read the five axes. Everything lands in the
    STABLE scratch directory (.hlsize-out/) — same tree, same paths,
    same bytes every time; a random temp name would only test the
    filesystem, never the compiler."""
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(workload))[0]
    out_c = os.path.join(out_dir, stem + ".c")
    out_bin = os.path.join(out_dir, stem + ".bin")
    compile_once(instrument, workload, out_c, root)
    try:
        p = subprocess.run([cc] + CC_FLAGS + ["-o", out_bin, out_c]
                           + CC_LIBS,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, cwd=root, timeout=600)
    except subprocess.TimeoutExpired:
        raise HlSizeError("the link of %s timed out after 600s" % stem)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()
        detail = tail[-1] if tail else "(no output)"
        raise HlSizeError(
            "the link of %s failed (exit %d): %s — a suite that does "
            "not link produces no sizes" % (stem, p.returncode, detail))
    text, data, bss = section_sizes(out_bin, cc)
    axes = {
        "c_bytes": os.path.getsize(out_c),
        "bin_bytes": os.path.getsize(out_bin),
        "text_bytes": text,
        "data_bytes": data,
        "bss_bytes": bss,
    }
    # The scratch emission is evidence, not a deliverable; the record
    # carries the numbers and the digests already pin the sources.
    for path in (out_bin,):
        try:
            os.unlink(path)
        except OSError:
            pass
    return axes


def measure_self(instrument, root, cc):
    """The native-only pseudo-workload: the compiler's OWN girth —
    bin/hlc.c (the bootstrap chain's emission), bin/hlc and its
    sections. This row answers "how fat is the tool this commit";
    the stage0 instrument has no built compiler to weigh, so the
    stage0 record simply does not carry the row (and the gate never
    compares the two instruments anyway)."""
    if instrument != "native":
        raise HlSizeError("hlc_self is a native-instrument workload — "
                          "the stage0 chain has no built compiler to "
                          "weigh", exit=2)
    hlc_c = os.path.join(root, "bin", "hlc.c")
    hlc_bin = os.path.join(root, NATIVE_COMPILER)
    if not os.path.isfile(hlc_bin):
        raise HlSizeError("the hlc_self workload needs %s — run "
                          "`make bootstrap` first" % hlc_bin, exit=2)
    # The pure-HLS bootstrap (Stage 142) no longer leaves its emission
    # in bin/ — it compares and deletes. The fixed-point law the
    # bootstrap enforces says the bytes bin/hlc emits for src/hlc.hls
    # ARE the chain's emission, so a missing bin/hlc.c is re-derived
    # here on demand, never guessed around.
    if not os.path.isfile(hlc_c):
        emit = subprocess.run([hlc_bin, HLC_SOURCE_ENTRY, hlc_c],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        if emit.returncode != 0 or not os.path.isfile(hlc_c):
            raise HlSizeError("the hlc_self workload could not re-derive "
                              "%s (hlc exited %d): %s"
                              % (hlc_c, emit.returncode,
                                 emit.stdout.strip()[:200]), exit=2)
    text, data, bss = section_sizes(hlc_bin, cc)
    return {
        "c_bytes": os.path.getsize(hlc_c),
        "bin_bytes": os.path.getsize(hlc_bin),
        "text_bytes": text,
        "data_bytes": data,
        "bss_bytes": bss,
    }


def measure_suite(root, instrument, members, grep, cc):
    """Every workload of the suite, plus hlc_self on the native
    instrument. The suite is compiled in sorted order; the axes are
    integers or the record does not exist."""
    workloads = {}
    for name, path in members:
        if grep and grep not in name:
            continue
        workloads[name] = measure_workload(instrument, path,
                                           os.path.join(root, SCRATCH),
                                           root, cc)
    if not workloads:
        raise HlSizeError("--grep %r matched no workload of the suite"
                          % grep, exit=2)
    if instrument == "native":
        workloads[SELF_WORKLOAD] = measure_self(instrument, root, cc)
    return workloads


# ---------------------------------------------------------------------------
# The ledger — one JSON object per line, append-only.
# ---------------------------------------------------------------------------

def load_history(path):
    """Parse the ledger. A line that does not parse, an object without
    a known type, a record without a commit — every one is a NAMED
    error naming the line number; the ledger is never guessed
    around."""
    if not os.path.exists(path):
        raise HlSizeError("the history ledger %s does not exist yet "
                          "(record something first)" % path, exit=2)
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise HlSizeError("history line %d is corrupt (%s) — "
                                  "the ledger is append-only; fix or "
                                  "remove the line by hand" % (i, exc))
            if not isinstance(obj, dict) or "type" not in obj:
                raise HlSizeError("history line %d carries no type" % i)
            if obj["type"] == "record":
                _validate_record(obj, "history line %d" % i)
            elif obj["type"] == "sanction":
                _validate_sanction(obj, "history line %d" % i)
            else:
                raise HlSizeError("history line %d has unknown type %r"
                                  % (i, obj["type"]))
            obj["_line"] = i
            entries.append(obj)
    return entries


def _validate_axes(w, where, name):
    """The five axes, integers, the honest zero law: c_bytes and
    bin_bytes must be positive (an emission with no bytes is a bug,
    a binary with no bytes is not a binary); the section axes may be
    legitimately zero (a program with no bss is a THIN program, not a
    broken one) — but the axis must be there, never absent."""
    for axis in AXES:
        if axis not in w:
            raise HlSizeError("%s: workload %s carries no %s — a "
                              "workload measured on four axes is not "
                              "measured" % (where, name, axis))
        v = w[axis]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise HlSizeError(
                "%s: workload %s axis %s is not a non-negative "
                "integer (%r) — sizes are bytes, and bytes are "
                "counted, never estimated" % (where, name, axis, v))
        if axis in ("c_bytes", "bin_bytes") and v == 0:
            raise HlSizeError(
                "%s: workload %s axis %s is zero — an empty emission "
                "or an empty binary is a bug, not a size"
                % (where, name, axis))


def _validate_record(rec, where):
    if not rec.get("commit") or not is_sha1(rec["commit"]):
        raise HlSizeError("%s: a record without a 40-hex "
                          "commit pin is not a per-commit record" % where)
    if rec.get("instrument") not in INSTRUMENTS:
        raise HlSizeError("%s: instrument %r is not one of %s"
                          % (where, rec.get("instrument"),
                             list(INSTRUMENTS)))
    if not rec.get("suite_digest") or not is_sha256(rec["suite_digest"]):
        raise HlSizeError("%s: a record without a suite "
                          "digest cannot be compared" % where)
    if not rec.get("source_digest") or not is_sha256(rec["source_digest"]):
        raise HlSizeError("%s: a record without a compiler source "
                          "digest does not name the compiler whose "
                          "output it measured" % where)
    if not rec.get("compiler_digest") \
            or not is_sha256(rec["compiler_digest"]):
        raise HlSizeError("%s: a record without a compiler binary "
                          "digest does not name the binary it "
                          "measured through" % where)
    env = rec.get("env")
    if not isinstance(env, dict):
        raise HlSizeError("%s: a record without a machine "
                          "fingerprint does not name the toolchain "
                          "that made its bytes" % where)
    for key in ENV_LAW_KEYS:
        if not env.get(key):
            raise HlSizeError("%s: the machine fingerprint carries "
                              "no %s — sizes from an unnamed %s are "
                              "not sizes" % (where, key, key))
    workloads = rec.get("workloads")
    if not isinstance(workloads, dict) or not workloads:
        raise HlSizeError("%s: a record with no workloads "
                          "measured nothing" % where)
    for name, w in sorted(workloads.items()):
        if not isinstance(w, dict):
            raise HlSizeError("%s: workload %s is not an axis "
                              "object" % (where, name))
        _validate_axes(w, where, name)
        if name == SELF_WORKLOAD and rec["instrument"] != "native":
            raise HlSizeError("%s: workload %s belongs to the native "
                              "instrument only" % (where, name))


def _validate_sanction(s, where):
    if not s.get("workload"):
        raise HlSizeError("%s: a sanction without a workload" % where)
    if not s.get("reason"):
        raise HlSizeError("%s: a sanction without a "
                          "written reason is not a sanction" % where)


def append_entry(path, entry):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json_line(entry))


def records_of(entries):
    return [e for e in entries if e["type"] == "record"]


def sanctions_of(entries):
    return [e for e in entries if e["type"] == "sanction"]


# ---------------------------------------------------------------------------
# record — measure now, or import a report, and pin it to a commit.
# ---------------------------------------------------------------------------

def env_fingerprint(root, instrument):
    """The machine the bytes were made on: cc version line, arch,
    recipe. The gate compares THESE keys between records — sizes from
    two toolchains are two different languages of the same program,
    and the gate refuses to translate between them."""
    cc = os.environ.get("CC", CC_DEFAULT)
    try:
        p = subprocess.run([cc, "--version"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=30)
        cc_line = p.stdout.strip().splitlines()[0] if p.stdout else cc
    except (OSError, subprocess.TimeoutExpired):
        cc_line = cc
    return {
        "cc": cc_line,
        "arch": platform.machine() or "unknown",
        "recipe": COMPILE_RECIPE,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "cpus": os.cpu_count() or 0,
    }


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_report(path, where):
    """The CI import path: a machine report is the measuring
    machine's word — it carries the axes AND the three digests
    (suite, compiler source, compiler binary) AND the machine
    fingerprint, because the machine that measured is the machine
    that can pin what it measured."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except OSError as exc:
        raise HlSizeError("cannot read the report: %s" % exc, exit=2)
    except ValueError as exc:
        raise HlSizeError("the report is not valid JSON: %s" % exc, exit=2)
    if not isinstance(report, dict) or report.get("tool") != TOOL:
        raise HlSizeError("%s: the report is not an hls-size report "
                          "(tool=%r)" % (where, report.get("tool")
                                         if isinstance(report, dict)
                                         else type(report).__name__), exit=2)
    if report.get("instrument") not in INSTRUMENTS:
        raise HlSizeError("%s: report instrument %r is not one of %s"
                          % (where, report.get("instrument"),
                             list(INSTRUMENTS)), exit=2)
    for key in ("suite_digest", "source_digest", "compiler_digest"):
        v = report.get(key)
        if not v or not is_sha256(v):
            raise HlSizeError("%s: the report carries no valid %s — a "
                              "machine report must pin what it measured"
                              % (where, key), exit=2)
    return report


def stale_binary_check(entries, new_rec):
    """THE STALE-BINARY LAW — the same floor hls-ctime stands on. A
    native record whose compiler digest was filed earlier against a
    DIFFERENT source digest is a lie: the same bin/hlc bytes cannot be
    the build of two different compiler sources, so the binary was
    not rebuilt for the commit it claims to measure. Doc-only commits
    leave the source digest unchanged, so an identical binary is
    expected there — the law refuses only the contradiction."""
    if new_rec["instrument"] != "native":
        return
    for r in records_of(entries):
        if r is new_rec:
            continue
        if r["instrument"] != "native":
            continue
        if r["compiler_digest"] == new_rec["compiler_digest"] \
                and r["source_digest"] != new_rec["source_digest"]:
            raise HlSizeError(
                "the stale-binary law: bin/hlc (sha256 %s) was already "
                "filed at commit %s against a different compiler source "
                "(%s vs %s) — the binary was not rebuilt for this commit; "
                "run `make bootstrap`, then record"
                % (short(new_rec["compiler_digest"]),
                   short(r["commit"]),
                   short(r["source_digest"]),
                   short(new_rec["source_digest"])))


def cmd_record(args):
    root = args.root or ROOT
    history = args.history or HISTORY_DEFAULT
    cc = os.environ.get("CC", CC_DEFAULT)

    # The pinning law: every record names one clean commit. Explicit
    # --commit wins (the CI import path); otherwise the ambient git
    # must supply a HEAD, and a dirty tree is a refusal, not a note.
    if args.commit:
        commit = args.commit.strip().lower()
        if not is_sha1(commit):
            raise HlSizeError("--commit must be a 40-hex sha (got %r)"
                              % args.commit, exit=2)
    else:
        commit = git_head(root)
        if not commit:
            raise HlSizeError(
                "%s is not a git work tree — pass --commit to pin the "
                "record explicitly" % root, exit=2)
        dirty = git(["status", "--porcelain", "-uno"], root)
        if dirty and not args.allow_dirty:
            raise HlSizeError(
                "the work tree carries tracked modifications — a "
                "per-commit record pins a clean commit "
                "(--allow-dirty to record anyway)")

    members = suite_members(root, args.suite)
    digest = members_digest(root, members)

    if args.from_report:
        report = read_report(args.from_report, "the report")
        instrument = report["instrument"]
        if report["suite_digest"] != digest:
            raise HlSizeError(
                "the report's suite digest (%s) does not match the "
                "local suite (%s) — the machine measured a different "
                "suite; re-baseline, do not file"
                % (short(report["suite_digest"]), short(digest)))
        source_digest = report["source_digest"]
        compiler_digest = report["compiler_digest"]
        workloads = report.get("workloads")
        if not isinstance(workloads, dict) or not workloads:
            raise HlSizeError("the report carries no measured "
                              "workloads", exit=2)
        env = report.get("env")
        if not isinstance(env, dict):
            raise HlSizeError("the report carries no machine "
                              "fingerprint — a machine report must "
                              "name the toolchain that made its "
                              "bytes", exit=2)
        source = "report %s" % args.from_report
    else:
        instrument = args.instrument
        if instrument == "native" \
                and not os.path.isfile(os.path.join(root, NATIVE_COMPILER)):
            raise HlSizeError(
                "the native instrument needs %s — run `make bootstrap` "
                "first (or record with --instrument stage0)"
                % NATIVE_COMPILER, exit=2)
        workloads = measure_suite(root, instrument, members,
                                  args.grep, cc)
        source_digest = compiler_source_digest(root)
        compiler_digest = compiler_binary_digest(root, instrument)
        env = env_fingerprint(root, instrument)
        source = instrument

    entry = {
        "type": "record",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "commit": commit,
        "instrument": instrument,
        "suite": args.suite,
        "suite_digest": digest,
        "source_digest": source_digest,
        "compiler_digest": compiler_digest,
        "env": env,
        "workloads": workloads,
        "note": args.note,
        "stamp": _utc_now() if args.stamp else None,
    }
    # The floor laws bite HERE, at record time — a record that its
    # own ledger would refuse never enters the ledger, and a stale
    # binary never gets the chance to pretend it was rebuilt.
    _validate_record(entry, "the new record")
    stale_binary_check(load_history(history) if os.path.exists(history)
                       else [], entry)
    append_entry(history, entry)
    _write_report(args, entry)
    print("recorded %s (%s) instrument=%s suite=%s workloads=%d "
          "machine=%s/%s" % (short(commit), commit, instrument,
                             args.suite, len(workloads),
                             env.get("cc", "?"), env.get("arch", "?")))
    for name in sorted(workloads):
        w = workloads[name]
        print("  %-16s c=%-9s bin=%-9s text=%-9s data=%-8s bss=%s"
              % (name, fmt_bytes(w["c_bytes"]),
                 fmt_bytes(w["bin_bytes"]), fmt_bytes(w["text_bytes"]),
                 fmt_bytes(w["data_bytes"]), fmt_bytes(w["bss_bytes"])))
    return 0


def _write_report(args, entry):
    """--json on record: file the filed entry beside the ledger (what
    CI keeps when the raw record matters)."""
    if not args.json:
        return
    with open(args.json, "w", encoding="utf-8") as fh:
        fh.write(json_dumps_sorted(entry))


# ---------------------------------------------------------------------------
# sanction — a written excuse, one workload at a time.
# ---------------------------------------------------------------------------

def cmd_sanction(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    known = sorted({n for r in records_of(entries)
                    for n in r["workloads"]})
    if args.workload not in known:
        raise HlSizeError(
            "unknown workload %r — the ledger knows: %s"
            % (args.workload, ", ".join(known) if known else "(none)"),
            exit=2)
    if not args.reason:
        raise HlSizeError("a sanction without a written reason is not a "
                          "sanction (--reason TEXT)", exit=2)
    entry = {
        "type": "sanction",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "workload": args.workload,
        "reason": args.reason,
        "commit": args.commit,
        "author": args.author or git_user(args.root or ROOT),
        "stamp": _utc_now() if args.stamp else None,
    }
    append_entry(history, entry)
    print("sanctioned %s (%s)" % (args.workload, args.author
                                  or git_user(args.root or ROOT)))
    return 0


# ---------------------------------------------------------------------------
# gate — the law: one unsanctioned regression fails.
# ---------------------------------------------------------------------------

def pick_baseline(new_rec, entries, want):
    """The instrument law and the machine law: comparisons happen only
    within the same instrument, the byte-identical suite AND the same
    machine fingerprint. Default baseline: the newest earlier record
    of that instrument."""
    recs = records_of(entries)
    idx = max(i for i, r in enumerate(recs) if r is new_rec)
    if want:
        cands = [r for r in recs if r["commit"].startswith(want)]
        if not cands:
            raise HlSizeError("no record carries commit prefix %r" % want,
                              exit=2)
        if len(cands) > 1:
            raise HlSizeError("commit prefix %r is ambiguous (%d "
                              "records)" % (want, len(cands)), exit=2)
        base = cands[0]
        if base is new_rec:
            raise HlSizeError("the baseline is the record itself — a "
                              "record does not regress against "
                              "itself", exit=2)
    else:
        earlier = [r for r in recs[:idx]
                   if r["instrument"] == new_rec["instrument"]
                   and r["suite_digest"] == new_rec["suite_digest"]]
        if not earlier:
            raise HlSizeError(
                "no earlier record of the same instrument to compare "
                "against (instrument=%s suite_digest=%s) — record one "
                "first" % (new_rec["instrument"],
                           new_rec["suite_digest"][:12]), exit=2)
        base = earlier[-1]
    if base["instrument"] != new_rec["instrument"]:
        raise HlSizeError(
            "different instruments: the baseline is %s, the record is "
            "%s — the bootstrap chain and the self-hosted compiler are "
            "different machines" % (base["instrument"],
                                    new_rec["instrument"]))
    if base["suite_digest"] != new_rec["suite_digest"]:
        raise HlSizeError(
            "the suite changed since the baseline (digest %s vs %s) — "
            "re-baseline, do not compare"
            % (base["suite_digest"][:12],
               new_rec["suite_digest"][:12]))
    for key in ENV_LAW_KEYS:
        if base["env"].get(key) != new_rec["env"].get(key):
            raise HlSizeError(
                "the machine changed since the baseline (%s %r vs %r) — "
                "sizes from two toolchains are not the same language; "
                "re-baseline on the new machine, do not compare"
                % (key, base["env"].get(key), new_rec["env"].get(key)))
    return base


def classify_axes(cur, base):
    """THE LAW — the integer byte compare, per axis. This is the
    whole classification: no samples, no statistics, no noise band.
    Returns (workload_class, per-axis detail)."""
    axes = {}
    worst = "no-change"
    saw_improved = False
    saw_regressed = False
    for axis in AXES:
        c = cur[axis]
        b = base[axis]
        if c > b:
            klass = "regressed"
            saw_regressed = True
        elif c < b:
            klass = "improved"
            saw_improved = True
        else:
            klass = "no-change"
        axes[axis] = {
            "cur": c, "base": b,
            "delta": c - b,
            "delta_pct": (c - b) * 100.0 / b if b else None,
            "class": klass,
        }
    # The worst-axis law: one axis growing regresses the workload;
    # a workload improves only when EVERY axis shrank-or-held and
    # at least one strictly shrank; everything else is no-change.
    if saw_regressed:
        worst = "regressed"
    elif saw_improved:
        worst = "improved"
    return worst, axes


def verdict_of(entries, baseline=None):
    """Classify the newest record against the chosen baseline and
    apply the sanctions. Returns the full machine verdict."""
    recs = records_of(entries)
    if not recs:
        raise HlSizeError("the ledger carries no records — nothing to "
                          "gate", exit=2)
    new_rec = recs[-1]
    base_rec = pick_baseline(new_rec, entries, baseline)

    active = {}
    for s in sanctions_of(entries):
        active.setdefault(s["workload"], s)

    rows = []
    for name in sorted(set(new_rec["workloads"])
                       | set(base_rec["workloads"])):
        if name in new_rec["workloads"] \
                and name in base_rec["workloads"]:
            klass, axes = classify_axes(new_rec["workloads"][name],
                                        base_rec["workloads"][name])
            row = {"workload": name, "class": klass, "axes": axes}
        elif name in new_rec["workloads"]:
            row = {"workload": name, "class": "new",
                   "axes": new_rec["workloads"][name]}
        else:
            row = {"workload": name, "class": "vanished"}
        if row["class"] in ("regressed", "vanished") and name in active:
            row["sanction"] = {"reason": active[name]["reason"],
                               "author": active[name].get("author"),
                               "commit": active[name].get("commit")}
        rows.append(row)

    unsanctioned = [r for r in rows
                    if r["class"] in ("regressed", "vanished")
                    and "sanction" not in r]
    verdict = {
        "tool": TOOL,
        "version": TOOL_VERSION,
        "verdict": "fail" if unsanctioned else "pass",
        "record": new_rec["commit"],
        "baseline": base_rec["commit"],
        "instrument": new_rec["instrument"],
        "suite": new_rec["suite"],
        "workloads": rows,
        "sanctioned": sorted(r["workload"] for r in rows
                             if "sanction" in r),
        "unsanctioned": sorted(r["workload"] for r in unsanctioned),
    }
    return verdict


def cmd_gate(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    v = verdict_of(entries, baseline=args.baseline)
    if not args.quiet:
        print("gate %s: record %s vs baseline %s (instrument %s, "
              "byte-exact law)"
              % (v["verdict"].upper(), short(v["record"]),
                 short(v["baseline"]), v["instrument"]))
        for r in v["workloads"]:
            line = "  %-16s %-9s" % (r["workload"], r["class"])
            if r["class"] in ("regressed", "no-change", "improved"):
                grown = ["%s %s" % (axis, fmt_delta(d["delta"]))
                         for axis, d in sorted(r["axes"].items())
                         if d["delta"]]
                line += "  %s" % (", ".join(grown)
                                  if grown else "all axes held")
            elif r["class"] == "new":
                line += "  (bin %s)" % fmt_bytes(
                    r["axes"].get("bin_bytes"))
            if "sanction" in r:
                line += ("  sanctioned: %s (%s)"
                         % (r["sanction"]["reason"],
                            r["sanction"].get("author")))
            print(line)
    if v["unsanctioned"]:
        print("GATE FAIL: %s grew without a sanction: %s"
              % (len(v["unsanctioned"]), ", ".join(v["unsanctioned"])))
    elif not args.quiet:
        print("GATE PASS: no unsanctioned growth across %d workloads"
              % len(v["workloads"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json_dumps_sorted(v))
    return 1 if v["unsanctioned"] else 0

# ---------------------------------------------------------------------------
# dashboard — the deterministic render of the ledger.
# ---------------------------------------------------------------------------

def sparkline(values):
    """A per-workload bin_bytes trend across records, oldest to
    newest. The series is normalized to [min, max] over 8 bands; a
    constant series is a run of mid bands (a wall of max-height
    blocks would read as a regression that is not there). The band
    is INTEGER half-up math — ((v-lo)*14 + (hi-lo)) // (2*(hi-lo))
    — the exact function the ok corpus fixture pins, so tool and
    fixture cannot drift."""
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return _SPARK_MID * len(values)
    span = hi - lo
    out = []
    for v in values:
        band = ((v - lo) * 14 + span) // (2 * span)
        out.append(_SPARK_BANDS[int(band)])
    return "".join(out)


def render_dashboard(entries, history_path, limit, stamp=None):
    """The whole document is a pure function of the ledger bytes and
    nothing else (stamp excepted). Sorted walks everywhere; the sha256
    of the ledger file pins what was rendered."""
    recs = records_of(entries)
    if not recs:
        raise HlSizeError("the ledger carries no records — nothing to "
                          "render", exit=2)
    try:
        with open(history_path, "rb") as fh:
            digest = sha256_hex(fh.read())
    except OSError as exc:
        raise HlSizeError("cannot read the ledger: %s" % exc, exit=2)

    new_rec = recs[-1]
    base = None
    verdict = None
    try:
        verdict = verdict_of(entries)
        base = pick_baseline(new_rec, entries, None)
    except HlSizeError:
        verdict = None
        base = None

    workload_names = sorted(set(new_rec["workloads"]))

    shown = recs[-limit:] if limit and limit > 0 else recs

    lines = []
    a = lines.append
    a("# HLS binary-size dashboard")
    a("")
    a("- Tool: %s %s" % (TOOL, TOOL_VERSION))
    a("- Ledger: `%s` (sha256 `%s`)" % (history_path, digest))
    a("- Records: %d (showing %d)" % (len(recs), len(shown)))
    a("- Newest record: `%s` (%d workloads, instrument %s)"
      % (new_rec["commit"], len(new_rec["workloads"]),
         new_rec["instrument"]))
    a("- Compiler source: `%s`" % new_rec["source_digest"])
    a("- Compiler binary: `%s`" % new_rec["compiler_digest"])
    a("- Machine: %s / %s (`%s`)"
      % (new_rec["env"].get("cc", "?"),
         new_rec["env"].get("arch", "?"),
         new_rec["env"].get("recipe", "?")))
    if base is not None:
        a("- Baseline: `%s`" % base["commit"])
    else:
        a("- Baseline: none (no comparable earlier record)")
    if verdict is not None:
        a("- Gate verdict: **%s**" % verdict["verdict"])
        if verdict["unsanctioned"]:
            a("- Unsanctioned: %s"
              % ", ".join(verdict["unsanctioned"]))
    else:
        a("- Gate verdict: n/a (no comparable earlier record)")
    if stamp:
        a("- Generated: %s" % stamp)
    a("")
    a("## Verdicts (newest record vs baseline, byte-exact)")
    a("")
    if verdict is None:
        a("No comparison yet: the newest record stands alone. Record "
          "twice on the same machine and the verdict table fills.")
    else:
        a("| workload | class | c_bytes | bin_bytes | text | data | "
          "bss |")
        a("|---|---|---|---|---|---|---|")
        for r in verdict["workloads"]:
            head = "| %s | %s" % (r["workload"], r["class"])
            if "sanction" in r:
                head += " (sanctioned)"
            if r["class"] in ("regressed", "no-change", "improved"):
                cells = []
                for axis in ("c_bytes", "bin_bytes", "text_bytes",
                             "data_bytes", "bss_bytes"):
                    d = r["axes"][axis]
                    cells.append("%s \u2192 %s (%s)"
                                 % (fmt_bytes(d["base"]),
                                    fmt_bytes(d["cur"]),
                                    fmt_delta(d["delta"])))
                a("%s | %s |" % (head, " | ".join(cells)))
            elif r["class"] == "new":
                w = r["axes"]
                a("%s | %s | %s | %s | %s | %s |"
                  % (head, fmt_bytes(w["c_bytes"]),
                     fmt_bytes(w["bin_bytes"]),
                     fmt_bytes(w["text_bytes"]),
                     fmt_bytes(w["data_bytes"]),
                     fmt_bytes(w["bss_bytes"])))
            else:
                a("%s | — | — | — | — | — |" % head)
    a("")
    a("## Trends (bin_bytes across records, oldest to newest)")
    a("")
    a("| workload | trend | %s |" % " | ".join(
        short(r["commit"], 7) for r in shown))
    a("|---|---|" + "---|" * len(shown))
    for name in workload_names:
        series = []
        for r in shown:
            w = r["workloads"].get(name)
            series.append(w["bin_bytes"] if w else None)
        known = [m for m in series if m]
        spark = sparkline(known) if known else "-"
        cells = " | ".join(
            (fmt_bytes(m) if m else "—") for m in series)
        a("| %s | %s | %s |" % (name, spark, cells))
    a("")
    a("## Sanctions")
    a("")
    sancs = sanctions_of(entries)
    if not sancs:
        a("None. Every byte of growth the ledger has seen was "
          "answered, not excused.")
    else:
        a("| workload | author | commit | reason |")
        a("|---|---|---|---|")
        seen = set()
        for s in sorted(sancs, key=lambda s: (s["workload"],
                                              s.get("reason") or "")):
            key = (s["workload"], s.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            a("| %s | %s | %s | %s |"
              % (s["workload"], s.get("author") or "unknown",
                 ("`%s`" % short(s["commit"])) if s.get("commit")
                 else "—",
                 (s.get("reason") or "").replace("|", "\\|")))
    a("")
    a("## Reading this dashboard")
    a("")
    a("- A record is one size measurement pinned to one clean commit: "
      "the tool compiled every workload of the tracked suite through "
      "one instrument, linked each emission with the Makefile's own "
      "recipe and read the five axes (emitted C bytes; binary bytes; "
      "the text/data/bss breakdown). The ledger is append-only and "
      "this document is a pure render of it (the sha256 above pins "
      "the bytes).")
    a("- Binary size is the axis with NO NOISE: the same sources on "
      "the same toolchain make the same bytes, so the gate keeps no "
      "samples and hides behind no noise band — one byte of growth "
      "is a regression the gate names. A workload regresses when ANY "
      "axis grows; it improves only when every axis shrank-or-held "
      "and at least one strictly shrank.")
    a("- The instrument is the compiler. `stage0` is the bootstrap "
      "chain (tools/hlinterp/boot.py running src/hlc.hls); `native` is "
      "bin/hlc — and the native record alone carries `hlc_self`, the "
      "compiler's own girth. Instruments are never compared to each "
      "other, and neither are machines: cc, arch and recipe are part "
      "of every record's fingerprint.")
    a("- Every record pins the compiler's identity: the sha256 of "
      "the compiler source and of the binary measured through. A "
      "native record whose binary was already filed against a "
      "different source is refused at record time (the stale-binary "
      "law) — bin/hlc must be rebuilt for the commit it measures.")
    a("- regressed or vanished workloads fail the gate unless a "
      "sanction names them. A sanction is a written excuse with an "
      "author — the register above is the audit trail.")
    a("")
    return "\n".join(lines)


def cmd_dashboard(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    doc = render_dashboard(entries, history, args.limit,
                           stamp=_utc_now() if args.stamp else None)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    if args.json:
        v = verdict_of(entries)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json_dumps_sorted(v))
    print("dashboard written to %s" % args.out)
    return 0


# ---------------------------------------------------------------------------
# history — read the ledger back.
# ---------------------------------------------------------------------------

def cmd_history(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    shown = entries
    if args.workload:
        shown = [e for e in entries
                 if (e["type"] == "record"
                     and e["workloads"].get(args.workload))
                 or (e["type"] == "sanction"
                     and e["workload"] == args.workload)]
    if args.json:
        out = []
        for e in shown:
            e2 = {k: v for k, v in e.items() if not k.startswith("_")}
            out.append(e2)
        sys.stdout.write(json_dumps_sorted(out))
        return 0
    for e in shown:
        if e["type"] == "record":
            print("line %-4d record  %s  %-7s suite=%s workloads=%d"
                  % (e["_line"], e["commit"], e["instrument"],
                     os.path.basename(e["suite"].rstrip("/")),
                     len(e["workloads"])))
        else:
            print("line %-4d sanction %s  %s"
                  % (e["_line"], e["workload"], e["reason"]))
    print("%d entries" % len(shown))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="hls-size",
        description="Halis per-commit binary-size regression ledger "
                    "(Stage 139). The compiler is the instrument; the "
                    "bytes are the measurement; this tool remembers "
                    "and judges them, byte-exact.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("record", help="measure and pin one commit")
    p.add_argument("--root", default=None,
                   help="repository root the record is pinned from "
                        "(default: the tool's repo)")
    p.add_argument("--suite", default=DEFAULT_SUITE,
                   help="workload suite directory (default: %s)"
                        % DEFAULT_SUITE)
    p.add_argument("--instrument", default="native",
                   choices=list(INSTRUMENTS),
                   help="which compiler to measure through (default: "
                        "native, bin/hlc; stage0 is the bootstrap "
                        "chain)")
    p.add_argument("--grep", default=None,
                   help="only measure workloads matching this substring")
    p.add_argument("--from-report", default=None, metavar="R.json",
                   help="import a machine report instead of measuring")
    p.add_argument("--commit", default=None,
                   help="pin this commit explicitly (the CI path)")
    p.add_argument("--note", default=None)
    p.add_argument("--allow-dirty", action="store_true",
                   help="record despite tracked modifications")
    p.add_argument("--stamp", action="store_true",
                   help="embed the wall-clock stamp in the entry")
    p.add_argument("--history", default=None)
    p.add_argument("--json", default=None)
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("sanction", help="excuse one workload, in writing")
    p.add_argument("--root", default=None,
                   help="repository root the author name is read from "
                        "(default: the tool's repo)")
    p.add_argument("workload")
    p.add_argument("--reason", default=None)
    p.add_argument("--commit", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--stamp", action="store_true")
    p.add_argument("--history", default=None)
    p.set_defaults(fn=cmd_sanction)

    p = sub.add_parser("gate", help="classify newest vs baseline; "
                                    "exit 1 on unsanctioned growth")
    p.add_argument("--baseline", default=None, metavar="SHA",
                   help="compare against this commit's record")
    p.add_argument("--history", default=None)
    p.add_argument("--json", default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("dashboard", help="render the ledger to "
                                         "markdown")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=DEFAULT_DASH_LIMIT)
    p.add_argument("--stamp", action="store_true")
    p.add_argument("--history", default=None)
    p.add_argument("--json", default=None)
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("history", help="list the ledger entries")
    p.add_argument("--workload", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--history", default=None)
    p.set_defaults(fn=cmd_history)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_usage(sys.stderr)
        return 2
    try:
        return args.fn(args)
    except HlSizeError as exc:
        sys.stderr.write("hls-size: %s\n" % exc)
        return exc.exit


if __name__ == "__main__":
    sys.exit(main())
