#!/usr/bin/env python3
"""Stage 96 acceptance gate — ELF symbol-table emission + debug-info
(DWARF 5).

Run with `make debuginfo-acceptance` (or
`python3 tests/debuginfo_acceptance.py`).

Seven sections:

  1. the CLI surface      — --debug refuses a stdout build (the #line
                            markers must name the generated C file),
                            --symbols demands a path, the usage names
                            both flags, and a flags run is DETERMINISTIC
                            (byte-identical C and manifest across runs)
  2. the line markers     — the C carries `#line` directives naming the
                            .hls source at the demo's fn and statement
                            lines, and hands the numbering back to the
                            generated C file between bodies; a build
                            WITHOUT --debug carries no markers at all
  3. the DWARF            — the C compiled with -g -gdwarf-5 produces
                            DWARF (CU version 5) whose decoded line
                            table names the .hls file, and addr2line
                            answers with the Halis file:line (wherever
                            binutils exists; an honest SKIP otherwise)
  4. the manifest shape   — every row is `<binding> <section> <symbol>
                            [<source>]`, the user functions carry their
                            file:line, and no junk names survive
  5. the image vs nm      — the freestanding x86-64 image (built the
                            Stage 93 way): every DEFINED symbol nm
                            reports is in the manifest (minus the
                            linker script's own symbols and GCC's
                            `.part.N` clones), the entry chain is
                            global text, and the image still exits 42;
                            the hosted image's hl_/usf_ symbols are
                            complete too, with the manifest's text/bss
                            classes agreeing with nm's letters
  6. the orchestrator     — hlcross --debug drives hlc --debug plus the
                            -g -gdwarf-5 link; the image carries
                            .debug_info naming the .hls source
  7. the no-libc mode     — the Stage 92 build accepts --debug and
                            --symbols too, and the tools stay clean on
                            the new sources
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_stage96_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/dbg_demo.hls"
TRIPLE = "x86_64-unknown-none"

# The demo's pinned lines (dbg_demo.hls): three fns whose fn line and
# statement lines the markers must name.
DEMO_MIX_LINE = 10
DEMO_GCD_LINE = 16
DEMO_MAIN_LINE = 27
DEMO_MIX_STMT = 12  # acc = acc + b


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def manifest_rows(path):
    rows = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.rstrip("\n")
        if not ln or ln.startswith("#"):
            continue
        rows.append(ln.split())
    return rows


IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ld_script_symbols(path):
    """Symbols the linker script assigns itself (`sym = .;` etc.)."""
    names = set()
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_$]*)\s*=", ln)
        if m:
            names.add(m.group(1))
    return names


def defined_nm_syms(binary):
    """{name: (binding_letter, class_letter)} of the image's defined
    symbols (uppercase = global, lowercase = local; 'u'/'U' excluded)."""
    out = {}
    r = run(["nm", binary])
    if r.returncode != 0:
        return None
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) == 3 and parts[1] not in ("U", "u"):
            letter = parts[1]
            binding = letter.upper() if letter.isupper() else letter.lower()
            out[parts[2]] = (binding, letter)
    return out


# ---------------------------------------------------------------------------
print("=== 1. the CLI surface: refusals, usage, determinism ===")
# ---------------------------------------------------------------------------

r = run(["./bin/hlc", "--debug", DEMO, "-"])
if r.returncode == 0:
    bad("cli: --debug accepted a stdout build")
elif "--debug needs a file output path" not in (r.stdout + r.stderr):
    bad("cli: the stdout refusal is not the staged words: %s"
        % (r.stdout + r.stderr).strip())
else:
    ok("cli: --debug refuses stdout (the markers must name the C file)")

r = run(["./bin/hlc", "--symbols"])
if r.returncode == 0:
    bad("cli: --symbols accepted a missing path")
elif "--symbols expects a manifest path" not in (r.stdout + r.stderr):
    bad("cli: the --symbols refusal is not the staged words: %s"
        % (r.stdout + r.stderr).strip())
else:
    ok("cli: --symbols demands its path")

r = run(["./bin/hlc"])
usage = r.stdout + r.stderr
if "--debug" in usage and "--symbols <manifest>" in usage:
    ok("cli: the usage names --debug and --symbols")
else:
    bad("cli: the usage does not name the new flags")

# Same output NAME in two directories: the #line resets name the
# output file's basename, so identical names are what byte-identity
# requires (the markers naming the file is the FEATURE).
dir_a = os.path.join(TMP, "det_a")
dir_b = os.path.join(TMP, "det_b")
os.makedirs(dir_a, exist_ok=True)
os.makedirs(dir_b, exist_ok=True)
c1 = os.path.join(dir_a, "out.c")
c2 = os.path.join(dir_b, "out.c")
s1 = os.path.join(TMP, "det1.sym")
s2 = os.path.join(TMP, "det2.sym")
a = run(["./bin/hlc", "--debug", "--symbols", s1, DEMO, c1])
b = run(["./bin/hlc", "--debug", "--symbols", s2, DEMO, c2])
if a.returncode != 0 or b.returncode != 0:
    bad("cli: a flags run failed (rc=%d/%d)" % (a.returncode, b.returncode))
elif open(c1).read() != open(c2).read():
    bad("cli: the --debug C is not deterministic across runs")
elif open(s1).read() != open(s2).read():
    bad("cli: the --symbols manifest is not deterministic across runs")
else:
    ok("cli: --debug + --symbols are deterministic (C and manifest)")

plain = os.path.join(TMP, "plain.c")
run(["./bin/hlc", DEMO, plain])
if re.search(r"^#line ", open(plain).read(), re.M):
    bad("cli: a plain build carries #line markers")
else:
    ok("cli: a build without --debug carries no markers (default unchanged)")

# ---------------------------------------------------------------------------
print("=== 2. the line markers: the .hls lines in the C ===")
# ---------------------------------------------------------------------------

c_dbg = os.path.join(TMP, "dbg.c")
run(["./bin/hlc", "--debug", DEMO, c_dbg])
src = open(c_dbg).read() if os.path.exists(c_dbg) else ""
markers = re.findall(r'^#line (\d+) (.+)$', src, re.M)
if not markers:
    bad("markers: no #line directives in the --debug C")
else:
    named = {}
    resets = []
    for num, lit in markers:
        f = lit.strip('"')
        if f.endswith(".hls"):
            named.setdefault(int(num), f)
        else:
            resets.append((num, f))
    want = {
        DEMO_MIX_LINE: "fn mix",
        DEMO_MIX_STMT: "mix's second statement",
        DEMO_GCD_LINE: "fn gcd",
        DEMO_MAIN_LINE: "fn main",
    }
    missing = [why for line, why in want.items() if line not in named]
    if missing:
        bad("markers: missing %s" % ", ".join(missing))
    elif any(not f.startswith("examples/dbg_demo.hls") for f in named.values()):
        bad("markers: a marker names an unexpected file: %s"
            % sorted(set(named.values())))
    else:
        ok("markers: fn lines (%d/%d/%d) and statement lines are mapped"
           % (DEMO_MIX_LINE, DEMO_GCD_LINE, DEMO_MAIN_LINE))
    if not resets:
        bad("markers: the numbering is never handed back to the C file")
    else:
        base = os.path.basename(c_dbg)
        wrong = [n for n, f in resets if f != base]
        if wrong:
            bad("markers: a reset names %r, not the generated C (%s)"
                % (wrong[0], base))
        else:
            ok("markers: %d resets hand the numbering back to %s"
               % (len(resets), base))

# ---------------------------------------------------------------------------
print("=== 3. the DWARF: version 5, the .hls lines, addr2line ===")
# ---------------------------------------------------------------------------

elf = os.path.join(TMP, "dbg.elf")
g = run(["gcc", "-O0", "-g", "-gdwarf-5", "-o", elf, c_dbg, "-lm", "-pthread"])
if g.returncode != 0:
    bad("dwarf: the -g -gdwarf-5 build failed: %s" % g.stderr.strip()[:300])
elif shutil.which("readelf") is None:
    print("  [SKIP] readelf not found — the DWARF dump is not checked")
elif shutil.which("addr2line") is None:
    print("  [SKIP] addr2line not found — the file:line answer is not checked")
else:
    info = run(["readelf", "--debug-dump=info", elf]).stdout
    if not re.search(r"Version:\s*5", info):
        bad("dwarf: the CU header does not say version 5")
    else:
        ok("dwarf: the unit's DWARF version is 5")
    dec = run(["readelf", "--debug-dump=decodedline", elf]).stdout
    hits = len(re.findall(r"dbg_demo\.hls\s+%d\b" % DEMO_MIX_LINE, dec))
    if "dbg_demo.hls" not in dec or hits == 0:
        bad("dwarf: the line table does not name dbg_demo.hls:%d"
            % DEMO_MIX_LINE)
    else:
        ok("dwarf: the line table names %s:%d"
           % (DEMO, DEMO_MIX_LINE))
    addr = run(["nm", elf]).stdout
    m = re.search(r"([0-9a-f]+) T usf_gcd", addr)
    if not m:
        bad("dwarf: usf_gcd has no address at -O0")
    else:
        a2l = run(["addr2line", "-e", elf, "-f", "0x" + m.group(1)]).stdout
        if "dbg_demo.hls:%d" % DEMO_GCD_LINE not in a2l:
            bad("dwarf: addr2line answers %r, want dbg_demo.hls:%d"
                % (a2l.strip().splitlines()[-1], DEMO_GCD_LINE))
        else:
            ok("dwarf: addr2line answers %s:%d for usf_gcd"
               % (DEMO, DEMO_GCD_LINE))

# ---------------------------------------------------------------------------
print("=== 4. the manifest shape: rows, sources, no junk ===")
# ---------------------------------------------------------------------------

sym_path = s1
rows = manifest_rows(sym_path)
if not rows:
    bad("manifest: no rows")
else:
    shape_ok = True
    names = []
    for parts in rows:
        if len(parts) not in (3, 4) or parts[0] not in ("global", "static") \
                or parts[1] not in ("text", "rodata", "data", "bss") \
                or not IDENT.match(parts[2]):
            shape_ok = False
            bad("manifest: a malformed row: %r" % " ".join(parts))
            break
        names.append(parts[2])
    if shape_ok:
        if len(names) != len(set(names)):
            bad("manifest: duplicate rows")
        else:
            ok("manifest: %d rows, every one `<binding> <section> <symbol>`"
               % len(names))
    by_name = {p[2]: p for p in rows}
    src_ok = True
    for cname, line in (("usf_mix", DEMO_MIX_LINE),
                        ("usf_gcd", DEMO_GCD_LINE),
                        ("usf_main", DEMO_MAIN_LINE)):
        row = by_name.get(cname)
        if not row or len(row) != 4 \
                or not row[3].endswith("dbg_demo.hls:%d" % line):
            bad("manifest: %s does not carry %s:%d (row=%r)"
                % (cname, DEMO, line, row))
            src_ok = False
    if src_ok:
        ok("manifest: the user fns carry their Halis source (file:line)")
    if by_name.get("main", ["", "", ""])[0:2] != ["global", "text"]:
        bad("manifest: main is not global text: %r" % by_name.get("main"))
    else:
        ok("manifest: main is global text")

# ---------------------------------------------------------------------------
print("=== 5. the image vs nm: the manifest IS the symbol table ===")
# ---------------------------------------------------------------------------

bare_c = os.path.join(TMP, "bare.c")
bare = os.path.join(TMP, "bare_x86_64")
run(["./bin/hlc", "--debug", "--symbols",
     os.path.join(TMP, "bare.sym"), "--target", TRIPLE, DEMO, bare_c])
g = run(["gcc", "-O2", "-mgeneral-regs-only", "-ffreestanding", "-nostdlib",
         "-nostartfiles", "-fno-stack-protector", "-fno-pie", "-no-pie",
         "-ffunction-sections", "-Wl,--gc-sections,-T,targets/%s.ld" % TRIPLE,
         "-o", bare, bare_c])
if g.returncode != 0:
    bad("image: the Stage 93 link failed: %s" % g.stderr.strip()[:300])
else:
    nm = defined_nm_syms(bare)
    if nm is None:
        bad("image: nm failed")
    else:
        ld = ld_script_symbols("targets/%s.ld" % TRIPLE)
        foreign = {n for n in nm if n.endswith(".part.0")} | ld
        missing = sorted(n for n in nm if n not in foreign
                         and n not in {p[2] for p in manifest_rows(
                             os.path.join(TMP, "bare.sym"))})
        if missing:
            bad("image: nm-defined symbols missing from the manifest: %s"
                % missing[:5])
        else:
            ok("image: every defined symbol (%d, minus the script's own "
               "and GCC's .part clones) is in the manifest" % len(nm))
        chain_ok = True
        for s in ("_start", "hl_boot", "usf_main", "main"):
            if s in nm and nm[s][0] != "T":
                bad("image: %s is %s, not global text" % (s, nm[s][0]))
                chain_ok = False
        if chain_ok:
            ok("image: the entry chain (_start, hl_boot, usf_main, main) "
               "is global text")
    rr = run([bare], timeout=60)
    if rr.returncode != 42:
        bad("image: the mapped build exits %d, not 42" % rr.returncode)
    else:
        ok("image: the --debug --symbols build still runs and exits 42")

    # The hosted image links the FULL runtime (--gc-sections keeps the
    # unreached parts out of the freestanding one) — the strongest
    # completeness check there is over the compiler-owned prefixes.
    full = os.path.join(TMP, "dbg_full.elf")
    hf = run(["gcc", "-O2", "-o", full, c_dbg, "-lm", "-pthread"])
    if hf.returncode != 0:
        bad("image: the hosted link failed")
    else:
        nm = defined_nm_syms(full)
        mine = {n for n in nm if n.startswith(("usf_", "hl_")) or n == "main"
                and nm[n][0] in ("T", "t")}
        rows = {p[2] for p in manifest_rows(sym_path)}
        missing = sorted(n for n in mine if n not in rows
                         and not n.endswith(".part.0"))
        if missing:
            bad("image: hosted hl_/usf_ symbols missing from the manifest: "
                "%s" % missing[:5])
        else:
            ok("image: every hosted hl_/usf_/main symbol is in the manifest")
        cls_ok = True
        for parts in manifest_rows(sym_path):
            name, sec, binding = parts[2], parts[1], parts[0]
            if name not in nm:
                continue  # inlined away by the C compiler — not hlc's call
            letter = nm[name][1]
            if sec == "text" and letter.upper() != "T":
                bad("image: text row %s is nm '%s'" % (name, letter))
                cls_ok = False
            if sec == "bss" and letter.upper() != "B":
                bad("image: bss row %s is nm '%s'" % (name, letter))
                cls_ok = False
        if cls_ok:
            ok("image: the manifest's text/bss classes agree with nm "
               "(wherever the symbol survived)")

# ---------------------------------------------------------------------------
print("=== 6. the orchestrator: hlcross --debug keeps the DWARF ===")
# ---------------------------------------------------------------------------

xb = os.path.join(TMP, "cross_dbg")
r = run(["python3", "tools/hlcross.py", "--debug", "--target", TRIPLE,
         DEMO, xb, "--keep-c", os.path.join(TMP, "cross_dbg.c")])
if r.returncode not in (0, 3):
    bad("orchestrator: hlcross --debug failed: %s"
        % (r.stdout + r.stderr).strip()[:300])
elif r.returncode == 3:
    print("  [SKIP] no linker for the triple — hlcross said so honestly")
elif shutil.which("readelf") is None:
    print("  [SKIP] readelf not found — the image's DWARF is not checked")
else:
    info = run(["readelf", "-S", xb]).stdout
    if ".debug_info" not in info:
        bad("orchestrator: the image has no .debug_info")
    else:
        dec = run(["readelf", "--debug-dump=decodedline", xb]).stdout
        if "dbg_demo.hls" not in dec:
            bad("orchestrator: the image's line table does not name the "
                ".hls source")
        else:
            ok("orchestrator: hlcross --debug keeps DWARF naming the "
               ".hls source")

# ---------------------------------------------------------------------------
print("=== 7. the no-libc mode and the tools ===")
# ---------------------------------------------------------------------------

nc = os.path.join(TMP, "nolibc.c")
ns = os.path.join(TMP, "nolibc.sym")
r = run(["./bin/hlc", "--no-libc", "--debug", "--symbols", ns, DEMO, nc])
if r.returncode != 0:
    bad("no-libc: --debug --symbols refused on the Stage 92 mode: %s"
        % (r.stdout + r.stderr).strip()[:200])
elif not os.path.exists(ns):
    bad("no-libc: the manifest was not written")
else:
    rows = manifest_rows(ns)
    if not any(p[2] == "_start" for p in rows):
        bad("no-libc: the crt0 _start is not in the manifest")
    elif "# target: hosted (no-libc runtime)" not in open(ns).read():
        bad("no-libc: the manifest does not name its mode")
    else:
        ok("no-libc: --debug --symbols work on the Stage 92 mode")

brc = run(["python3", "boot/boot.py", "--check", DEMO])
if brc.returncode != 0:
    bad("front-end: boot.py --check rejects the demo: %s"
        % (brc.stdout + brc.stderr).strip()[:200])
else:
    ok("front-end: the demo is an ordinary crate to the checker")

fmt = run(["python3", "tools/hlfmt.py", DEMO])
if fmt.returncode != 0 or not fmt.stdout.strip():
    bad("tools: hlfmt failed on the demo")
else:
    ok("tools: hlfmt formats the demo")

lint = run(["python3", "tools/hllint.py", DEMO])
if lint.returncode != 0:
    bad("tools: hllint findings on the demo:\n%s"
        % (lint.stdout + lint.stderr).strip()[:300])
else:
    ok("tools: hllint clean on the demo")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 96 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
