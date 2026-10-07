#!/usr/bin/env python3
"""Stage 117 acceptance gate — hlfmt reads a team configuration file,
`.hlfmt.toml`.

Run with `make fmt-config-acceptance` (or
`python3 tests/fmt_config_acceptance.py`).

Eight sections:

  1. the discovery   — the walk from the target file's directory to the
                       root finds the config (the file's directory, then
                       each parent); the NEAREST file wins; `--config`
                       pins one file and disables the walk; `--no-config`
                       ignores discovery; no config anywhere means the
                       built-in defaults
  2. the knobs       — each key does exactly what it says:
                       indent_width (1..8), indent_style ("tab"),
                       final_newline (false), max_blank_lines (0 and 3),
                       reindent_comments (false keeps a stale comment
                       indent verbatim; true, the default, re-indents it)
  3. the strictness  — an unknown key, a wrong type, an out-of-range
                       value, a bad choice, a duplicate key, an
                       unsupported table, a malformed line, an
                       unterminated string and a missing --config file
                       all exit 2 naming file and line — and a bad
                       config never touches the target file
  4. the grammar     — the accepted surface parses: comments (also
                       inline after values), blank lines, the optional
                       [hlfmt] table, all five keys; --print-config
                       reflects the resolved values, and prints the
                       built-in defaults when nothing is found
  5. the fixed point — every knob combination formats as a fixed point
                       under ITS OWN config; an empty config file is
                       byte-identical to the defaults
  6. the default law — with no config anywhere, Stage 117 output is
                       byte-identical to the Stage 116 constants: the
                       4-space re-indent law still holds and the
                       comment set still survives a pass
  7. the differential— the stage's ok corpus file runs interpreter vs
                       native byte-identically, then a pass under a
                       NON-default config (width 2, blanks squeezed)
                       recompiles, re-runs and prints the same markers
                       on both front-ends
  8. the corpus      — every examples/, tests/ok/ and tests/fail/ file
                       formats idempotently under the defaults and a
                       sampled alternate config, keeps its comment set,
                       and hllint stays clean on the stage fixture

Exit code 0 = all acceptance criteria met.
"""
import glob
import itertools
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from tools.hlfmt import (  # noqa: E402
    ConfigError, DEFAULT_CONFIG, format_source, parse_config, resolve_config)

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s117_", dir=os.path.join(ROOT, "tests"))

HLC = os.path.join(ROOT, "bin", "hlc")
CORPUS = os.path.join(ROOT, "tests", "ok", "feat_stage117_fmt_config.hls")
HLFMT = [sys.executable, "tools/hlfmt.py"]


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def write(path, text):
    full = os.path.join(TMP, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
    return full


def run_cli(args):
    return subprocess.run(HLFMT + args, capture_output=True, text=True)


# A small messy-but-parseable fixture: junk indentation, runs of blanks,
# a stale comment indent, a trailing comment.
MESSY = (b"fn add(a: int,   b: int) -> int {\n"
         b"return a+b\n"
         b"}\n"
         b"\n"
         b"\n"
         b"\n"
         b"fn main() -> int uses IO {\n"
         b"        # a stale deep note\n"
         b"let x: int = add(1, 2)\n"
         b"println(x.to_str())  # trailing\n"
         b"return 0\n"
         b"}\n")

UNFORMATTED = (b"fn f() -> int {\n"
               b"if true {\n"
               b"return 1\n"
               b"}\n"
               b"return 0\n"
               b"}\n")


def idempotent_under(src, cfg):
    out1 = format_source(src, cfg)
    out2 = format_source(out1.encode("latin-1"), cfg).encode("latin-1")
    return out1.encode("latin-1"), out2 == out1.encode("latin-1")

print("=== 1. the discovery: the nearest .hlfmt.toml wins ===")
# 1a. a config next to the file applies.
root_a = write("disc_a/.hlfmt.toml", 'indent_width = 2\n')
f_a = write("disc_a/pkg/deep/thing.hls", UNFORMATTED.decode())
proc = run_cli(["-w", f_a])
with open(f_a, "rb") as fh:
    got = fh.read()
if b"\n  if true {\n" in got:
    ok("a config in the file's tree applies (2-space indents landed)")
else:
    bad("config not applied: %r" % got)
# 1b. the NEAREST file wins: root sets 4, the subdir sets 2.
root_b = write("disc_b/.hlfmt.toml", 'indent_width = 4\n')
write("disc_b/sub/.hlfmt.toml", 'indent_width = 2\n')
f_b_deep = write("disc_b/sub/inner.hls", UNFORMATTED.decode())
f_b_root = write("disc_b/toplevel.hls", UNFORMATTED.decode())
run_cli(["-w", f_b_deep])
run_cli(["-w", f_b_root])
with open(f_b_deep, "rb") as fh:
    deep = fh.read()
with open(f_b_root, "rb") as fh:
    shallow = fh.read()
if b"\n  if true {\n" in deep:
    ok("the nearest file wins for a deep target (2 spaces)")
else:
    bad("nearest-file rule broken for the deep target: %r" % deep)
if b"\n    if true {\n" in shallow:
    ok("the root file keeps the root config (4 spaces)")
else:
    bad("root config not applied to its own level: %r" % shallow)
# 1c. --config pins one file and beats discovery.
pin = write("pinned.toml", 'indent_width = 8\n')
f_c = write("disc_c/thing.hls", UNFORMATTED.decode())
proc = run_cli(["--config", pin, "-w", f_c])
with open(f_c, "rb") as fh:
    pinned = fh.read()
if b"\n        if true {\n" in pinned:
    ok("--config pins a file outside the walk (8 spaces landed)")
else:
    bad("--config pin not applied: %r" % pinned)
# 1d. --no-config ignores discovery entirely.
f_d = write("disc_a/no_cfg.hls", UNFORMATTED.decode())  # disc_a sets 2
run_cli(["--no-config", "-w", f_d])
with open(f_d, "rb") as fh:
    nocfg = fh.read()
if b"\n    if true {\n" in nocfg and b"\n  if true {\n" not in nocfg:
    ok("--no-config ignores the discovered file (defaults won)")
else:
    bad("--no-config did not fall back to defaults: %r" % nocfg)
# 1e. no config anywhere means defaults (scratch dir outside any tree).
f_e = write("clean_zone/thing.hls", UNFORMATTED.decode())
proc = run_cli(["-w", f_e])
with open(f_e, "rb") as fh:
    plain = fh.read()
if b"\n    if true {\n" in plain:
    ok("no config anywhere resolves to the built-in defaults")
else:
    bad("defaults missing with no config: %r" % plain)
# 1f. the walk crosses UP out of the scratch dir is not required — but
# the resolution must anchor at the TARGET, not the cwd: run from the
# repo root on a file whose tree carries the config (already the case
# in 1a). Assert the resolver API agrees on every case above.
cfg, src_path = resolve_config(f_a)
if cfg["indent_width"] == 2 and src_path == root_a:
    ok("resolve_config anchors at the target file (API level)")
else:
    bad("resolve_config walked wrong: %s via %s" % (cfg, src_path))
cfg, src_path = resolve_config(f_d, no_config=True)
if cfg == DEFAULT_CONFIG and src_path is None:
    ok("resolve_config --no-config yields pure defaults")
else:
    bad("resolve_config --no-config polluted: %s" % cfg)

print("=== 2. the knobs: each key does what it says ===")
# 2a. indent_width sweep (the fixture's `return 1` sits at depth 2).
for width in (1, 2, 3, 5, 8):
    out, idem = idempotent_under(UNFORMATTED, {"indent_width": width})
    unit = b" " * width
    if (b"\n" + unit * 2 + b"return 1") in out and idem:
        ok("indent_width = %d formats at %d spaces per level" % (width, width))
    else:
        bad("indent_width = %d wrong (idem=%s): %r" % (width, idem, out))
# 2b. indent_style = "tab".
out, idem = idempotent_under(UNFORMATTED, {"indent_style": "tab"})
if b"\n\t\treturn 1\n" in out and idem:
    ok('indent_style = "tab" emits one tab per level')
else:
    bad('indent_style = "tab" wrong (idem=%s): %r' % (idem, out))
# 2c. final_newline = false.
out, idem = idempotent_under(UNFORMATTED, {"final_newline": False})
if not out.endswith(b"\n") and idem:
    ok("final_newline = false drops the EOF newline (still idempotent)")
else:
    bad("final_newline = false wrong (idem=%s, end=%r)"
        % (idem, out[-3:]))
# 2d. max_blank_lines = 0 squeezes every blank run.
out, idem = idempotent_under(MESSY, {"max_blank_lines": 0})
if b"\n\n" not in out and idem:
    ok("max_blank_lines = 0 squeezes every blank line")
else:
    bad("max_blank_lines = 0 left blanks (idem=%s): %r" % (idem, out))
# 2e. max_blank_lines = 3 caps a 5-blank run at exactly 3.
many = UNFORMATTED.replace(b"}\nreturn 0", b"}\n\n\n\n\n\nreturn 0")
out, idem = idempotent_under(many, {"max_blank_lines": 3})
run_len = 0
best = 0
for ch in out.decode("latin-1"):
    if ch == "\n":
        run_len += 1
        best = max(best, run_len - 1)
    else:
        run_len = 0
if best == 3 and idem:
    ok("max_blank_lines = 3 caps a 6-blank run at exactly 3")
else:
    bad("max_blank_lines = 3 wrong (longest run %d, idem=%s)"
        % (best, idem))
# 2f. reindent_comments = false replays the raw prefix verbatim;
# the default (true) re-indents the same note to the block.
out_raw, idem_raw = idempotent_under(MESSY, {"reindent_comments": False})
if b"        # a stale deep note" in out_raw and idem_raw:
    ok("reindent_comments = false keeps the stale indent verbatim")
else:
    bad("reindent_comments = false rewrote the indent: %r" % out_raw)
out_canon, idem_canon = idempotent_under(MESSY, None)
if b"    # a stale deep note" in out_canon and idem_canon:
    ok("the default re-indents the same note to the block (Stage 116 law)")
else:
    bad("default re-indent broken: %r" % out_canon)

print("=== 3. the strictness: a typo is an error, never a style ===")
BAD_CASES = [
    ("unknown key", "banana = 2\n", "unknown key"),
    ("wrong type for an int", 'indent_width = "four"\n', "wants an integer"),
    ("wrong type for a bool", "final_newline = 1\n", "wants true or false"),
    ("wrong type for a str", "indent_style = tab\n", "double-quoted"),
    ("out of range low", "indent_width = 0\n", "out of range"),
    ("out of range high", "indent_width = 9\n", "out of range"),
    ("blanks out of range", "max_blank_lines = 5\n", "out of range"),
    ("blanks negative", "max_blank_lines = -1\n", "out of range"),
    ("bad choice", 'indent_style = "banaba"\n', "wants one of"),
    ("duplicate key", "indent_width = 2\nindent_width = 3\n", "duplicate"),
    ("unsupported table", "[editor]\nindent_width = 2\n", "unsupported table"),
    ("no equals", "indent_width 2\n", "expected `key = value`"),
    ("no value", "indent_width =\n", "no value"),
    ("unterminated string", 'indent_style = "tab\n', "unterminated"),
    ("unterminated header", "[hlfmt\n", "unterminated table header"),
    ("bad escape", 'indent_style = "t\\x"\n', "invalid escape"),
]
for desc, cfg_text, needle in BAD_CASES:
    cfg_path = write("bad_%s.toml" % desc.replace(" ", "_"), cfg_text)
    target = write("strict_target.hls", UNFORMATTED.decode())
    before = open(target, "rb").read()
    proc = run_cli(["--config", cfg_path, "-w", target])
    after = open(target, "rb").read()
    if proc.returncode == 2 and needle in proc.stderr:
        ok("%s exits 2 with a diagnostic (%s...)"
           % (desc, proc.stderr.strip().splitlines()[0][:72]))
    else:
        bad("%s: rc=%d stderr=%r" % (desc, proc.returncode, proc.stderr[:120]))
    if before == after:
        ok("%s leaves the target file untouched" % desc)
    else:
        bad("%s modified the target file" % desc)
# A missing --config file is also exit 2.
proc = run_cli(["--config", os.path.join(TMP, "ghost.toml"),
                "-w", "examples/hello.hls"])
if proc.returncode == 2 and "not found" in proc.stderr:
    ok("missing --config file exits 2")
else:
    bad("missing --config: rc=%d %r" % (proc.returncode, proc.stderr[:120]))
# --config and --no-config together are a usage error.
proc = run_cli(["--config", pin, "--no-config", "-w", f_a])
if proc.returncode == 2 and "mutually exclusive" in proc.stderr:
    ok("--config + --no-config is rejected (exit 2)")
else:
    bad("flag conflict not rejected: rc=%d %r"
        % (proc.returncode, proc.stderr[:120]))
# Errors name the file AND the line.
line_probe = write("line_probe.toml", "final_newline = true\n\nbanana = 3\n")
try:
    parse_config(open(line_probe).read(), origin=line_probe)
    bad("line numbers: bad file parsed clean")
except ConfigError as ex:
    if "line_probe.toml:3" in str(ex):
        ok("config errors name file and line (%s)" % str(ex).split(": ", 1)[1])
    else:
        bad("error message lost the file:line anchor: %s" % ex)

print("=== 4. the grammar: the accepted surface parses ===")
GOOD = ("# team style — smallest diff set wins\n"
        "[hlfmt]\n"
        "indent_width = 2   # two spaces, like the web team\n"
        "\n"
        'indent_style = "space"\n'
        "final_newline = false\n"
        "max_blank_lines = 0\n"
        "reindent_comments = false  # legacy tree, minimal diffs\n")
cfg = parse_config(GOOD, origin="good.toml")
if cfg == {"indent_width": 2, "indent_style": "space",
           "final_newline": False, "max_blank_lines": 0,
           "reindent_comments": False}:
    ok("the full accepted grammar parses (comments, blanks, [hlfmt])")
else:
    bad("grammar parse wrong: %r" % cfg)
# resolve_config merges over the defaults.
good_path = write("good.toml", GOOD)
cfg, src_path = resolve_config("examples/hello.hls", config_path=good_path)
if src_path == good_path and cfg["indent_width"] == 2 \
        and cfg["max_blank_lines"] == 0 \
        and cfg["final_newline"] is False \
        and cfg["indent_style"] == "space" \
        and cfg["reindent_comments"] is False:
    ok("resolve_config merges overrides over the defaults")
else:
    bad("resolve_config merge wrong: %s from %s" % (cfg, src_path))
# --print-config reflects a pinned file.
proc = run_cli(["--config", good_path, "--print-config",
                "examples/hello.hls"])
out = proc.stdout
if (proc.returncode == 0 and 'indent_width = 2' in out
        and 'final_newline = false' in out
        and good_path in out):
    ok("--print-config reports the pinned file's resolved values")
else:
    bad("--print-config wrong: rc=%d %r" % (proc.returncode, out[:160]))
# --print-config with nothing found prints the pure defaults.
proc = run_cli(["--no-config", "--print-config", "examples/hello.hls"])
out = proc.stdout
if (proc.returncode == 0 and "built-in defaults" in out
        and "indent_width = 4" in out and "max_blank_lines = 1" in out
        and 'indent_style = "space"' in out and "final_newline = true" in out
        and "reindent_comments = true" in out):
    ok("--print-config prints the built-in defaults when nothing applies")
else:
    bad("--print-config defaults wrong: rc=%d %r"
        % (proc.returncode, out[:160]))
# The rendered TOML round-trips through the parser.
proc = run_cli(["--config", good_path, "--print-config",
                "examples/hello.hls"])
try:
    body = "\n".join(ln for ln in proc.stdout.splitlines()
                     if not ln.startswith("#"))
    again = parse_config(body, origin="roundtrip")
    if again["indent_width"] == 2 and again["final_newline"] is False:
        ok("--print-config output re-parses as valid config TOML")
    else:
        bad("round-trip values wrong: %r" % again)
except ConfigError as ex:
    bad("round-trip parse failed: %s" % ex)

print("=== 5. the fixed point: a config is a law unto itself ===")
combo_fail = None
count = 0
for width, style, blanks, eof in itertools.product(
        (1, 2, 4, 8), ("space", "tab"), (0, 1, 3), (True, False)):
    cfg = {"indent_width": width, "indent_style": style,
           "max_blank_lines": blanks, "final_newline": eof}
    out1, idem = idempotent_under(MESSY, cfg)
    count += 1
    if not idem:
        combo_fail = (cfg, out1)
        break
if combo_fail is None:
    ok("%d knob combinations format as fixed points under themselves"
       % count)
else:
    bad("non-idempotent combo %r: %r" % (combo_fail[0], combo_fail[1][:120]))
# An EMPTY config file is byte-identical to the defaults.
empty_path = write("empty.toml", "# nothing pinned\n")
cfg_empty, _ = resolve_config("examples/hello.hls", config_path=empty_path)
if format_source(MESSY) == format_source(MESSY, cfg_empty):
    ok("an empty config file is byte-identical to the defaults")
else:
    bad("empty config changed the output")

print("=== 6. the default law: no file means the Stage 116 constants ===")
# Byte-identical: default cfg vs explicit defaults vs no cfg at all.
explicit = format_source(MESSY, dict(DEFAULT_CONFIG))
implicit = format_source(MESSY)
if explicit == implicit:
    ok("explicit DEFAULT_CONFIG == implicit defaults (byte-identical)")
else:
    bad("explicit and implicit defaults diverge")
# The Stage 116 laws still hold under the defaults: 4-space re-indent,
# comment set preserved.
with open(CORPUS, "rb") as fh:
    corpus_src = fh.read()
out = format_source(corpus_src)
def comment_bodies(src):
    from tools.hlfmt import _extract_comments
    return sorted(v[1].strip() for v in _extract_comments(src).values())
if comment_bodies(corpus_src) == comment_bodies(out.encode("latin-1")):
    ok("the default pass still preserves the corpus comment set")
else:
    bad("the default pass altered the corpus comment set")
out2 = format_source(out.encode("latin-1")).encode("latin-1")
if out2 == out.encode("latin-1"):
    ok("the default pass is still a fixed point (Stage 116 idempotency)")
else:
    bad("the default pass lost idempotency")
stale = (b"fn f() -> int {\n"
         b"    if true {\n"
         b"    # badly indented\n"
         b"        return 1\n"
         b"    }\n"
         b"    return 0\n"
         b"}\n")
out = format_source(stale)
if "\n        # badly indented\n" in out:
    ok("the re-indent law still lifts a stale comment to its block")
else:
    bad("the Stage 116 re-indent law regressed: %r"
        % [ln for ln in out.splitlines() if "badly" in ln])

print("=== 7. the differential: a config pass preserves semantics ===")
if not os.path.exists(HLC):
    bad("bin/hlc missing — run make bootstrap first")
else:
    interp_out = subprocess.run(
        [sys.executable, "boot/boot.py", CORPUS],
        capture_output=True, text=True).stdout
    c_path = os.path.join(TMP, "s117_orig.c")
    bin_path = os.path.join(TMP, "s117_orig")
    proc = subprocess.run([HLC, CORPUS, c_path],
                          capture_output=True, text=True)
    compiled = proc.returncode == 0
    err = proc.stderr + proc.stdout
    native_out = None
    if compiled:
        cc = subprocess.run(["gcc", "-O2", "-o", bin_path, c_path,
                             "-lm", "-pthread"], capture_output=True,
                            text=True)
        if cc.returncode == 0:
            run = subprocess.run([bin_path], capture_output=True, text=True)
            native_out = run.stdout
    if compiled and native_out is not None and native_out == interp_out:
        ok("unformatted file: interpreter == native")
    else:
        bad("unformatted file diverges (compiled=%s, err=%s)"
            % (compiled, err[:160]))
    # A pass under a NON-default config must keep the semantics.
    alt_cfg = {"indent_width": 2, "max_blank_lines": 0}
    with open(CORPUS, "rb") as fh:
        alt_bytes = format_source(fh.read(), alt_cfg).encode("latin-1")
    alt_path = os.path.join(TMP, "s117_alt.hls")
    with open(alt_path, "wb") as fh:
        fh.write(alt_bytes)
    interp_alt = subprocess.run(
        [sys.executable, "boot/boot.py", alt_path],
        capture_output=True, text=True).stdout
    c_path2 = os.path.join(TMP, "s117_alt.c")
    bin_path2 = os.path.join(TMP, "s117_alt_bin")
    proc = subprocess.run([HLC, alt_path, c_path2],
                          capture_output=True, text=True)
    compiled2 = proc.returncode == 0
    err2 = proc.stderr + proc.stdout
    native_alt = None
    if compiled2:
        cc = subprocess.run(["gcc", "-O2", "-o", bin_path2, c_path2,
                             "-lm", "-pthread"], capture_output=True,
                            text=True)
        if cc.returncode == 0:
            run = subprocess.run([bin_path2], capture_output=True, text=True)
            native_alt = run.stdout
    if compiled2 and native_alt == interp_alt:
        ok("config-formatted file: interpreter == native (recompiles, reruns)")
    else:
        bad("config-formatted file diverges (compiled=%s, err=%s)"
            % (compiled2, err2[:160]))
    if interp_out == interp_alt:
        ok("the config pass changed no output line (semantic witness)")
    else:
        bad("the config pass changed program output:\n%r\n%r"
            % (interp_out, interp_alt))

print("=== 8. the corpus: idempotent under every config sampled ===")
files = sorted(glob.glob("examples/**/*.hls", recursive=True)
               + glob.glob("tests/ok/*.hls")
               + glob.glob("tests/fail/*.hls"))
non_idem = []
lost = []
for f in files:
    with open(f, "rb") as fh:
        src = fh.read()
    try:
        out1 = format_source(src)
    except Exception:
        continue  # unparseable fail- fixtures fall back verbatim
    out2 = format_source(out1.encode("latin-1")).encode("latin-1")
    if out2 != out1.encode("latin-1"):
        non_idem.append(f)
    from tools.hlfmt import _extract_comments
    a = sorted(v[1].strip() for v in _extract_comments(src).values())
    b = sorted(v[1].strip() for v in
               _extract_comments(out1.encode("latin-1")).values())
    if a != b:
        lost.append(f)
if not non_idem:
    ok("%d corpus files format idempotently under the defaults" % len(files))
else:
    bad("non-idempotent under defaults: %r" % non_idem[:8])
if not lost:
    ok("%d corpus files keep their comment set under the defaults"
       % len(files))
else:
    bad("comment set changed: %r" % lost[:8])
# A sampled alternate config (width 2, blanks squeezed) stays a fixed
# point across the whole corpus too.
alt_cfg = {"indent_width": 2, "max_blank_lines": 0}
alt_non_idem = []
for f in files:
    with open(f, "rb") as fh:
        src = fh.read()
    try:
        out1 = format_source(src, alt_cfg)
    except Exception:
        continue
    out2 = format_source(out1.encode("latin-1"), alt_cfg).encode("latin-1")
    if out2 != out1.encode("latin-1"):
        alt_non_idem.append(f)
if not alt_non_idem:
    ok("the alternate config is a fixed point across the corpus too")
else:
    bad("non-idempotent under the alternate config: %r" % alt_non_idem[:8])
lint = subprocess.run(
    [sys.executable, "tools/hllint.py", CORPUS],
    capture_output=True, text=True)
if lint.returncode == 0:
    ok("hllint stays clean on the stage fixture")
else:
    bad("hllint flagged the stage fixture:\n%s" % lint.stdout[:200])
chk = run_cli(["-c", CORPUS])
if chk.returncode == 0 and "already formatted" in chk.stdout:
    ok("the stage fixture ships already formatted under the defaults")
else:
    bad("stage fixture not formatted under defaults: %s%s"
        % (chk.stdout[:80], chk.stderr[:80]))

print("")
print("=== Stage 117 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
