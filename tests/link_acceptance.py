#!/usr/bin/env python3
"""Stage 84 acceptance gate — linker-script integration + custom sections.

Run with `make link-acceptance` (or `python3 tests/link_acceptance.py`).

Seven sections, matching the shape of the other OS-dev gates:

  1. resolution + guards      — core.section is importable, the
                                #[section]/#[align]/#![link_script]
                                attributes survive a standalone parse,
                                and the crate attribute is
                                entry-file-only
  2. standalone parse         — core/section.hls declares the expected
                                enums / structs / functions and
                                imports only core.option + core.result
  3. Stage-0 enforcement      — the no_std ok-test is clean on the
                                interpreter, the demo runs, and all 13
                                fail programs are rejected with their
                                own diagnostic by BOTH compilers
  4. behaviour probes         — name validation, alignment rules,
                                section kinds, the script reader, the
                                coverage rules and the placement
                                solver, exercised through small HLS
                                programs whose exit code names the
                                first failing assertion
  5. self-hosted emission     — the C carries the section/aligned
                                attributes, the freestanding TU is
                                shaped correctly, and the ok-test
                                links -nostdlib
  6. hlfmt + linter           — the formatter round-trips the new
                                attributes and the linter stays clean
  7. --audit                  — both compilers report the linker
                                script and the annotation counts
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
# The scratch directory lives INSIDE the repo so that `core.*` imports
# and the relative `#![link_script("...")]` paths of the gate's own
# programs resolve the same way they do for a real crate. (The
# interpreters resolve `core.` by walking up from the entry file.)
TMP = tempfile.mkdtemp(prefix="_gate_link_", dir=os.path.join(ROOT, "tests", "stage84"))

OK_TEST = "tests/ok/feat_stage84_section.hls"
DEMO = "examples/section_demo.hls"
CORE = "core/section.hls"
SCRIPT = "tests/stage84/place.ld"
NOSECTIONS = "tests/stage84/no_sections.ld"

# fail program -> (needle, description)
FAIL_PROGRAMS = [
    ("fail_stage84_section_badchar", "may only contain letters",
     "a space in a section name"),
    ("fail_stage84_section_start", "must start with",
     "a leading digit"),
    ("fail_stage84_section_dupdot", "must not contain '..'",
     "'..' in a section name"),
    ("fail_stage84_section_reserved", "compiler-owned output section",
     "a reserved output section"),
    ("fail_stage84_section_long", "the limit is 64",
     "an over-long section name"),
    ("fail_stage84_section_notstr", "expects a string literal",
     "a computed section name"),
    ("fail_stage84_section_dup", "appears more than once",
     "two #[section] on one fn"),
    ("fail_stage84_section_inline", "mutually exclusive",
     "#[section] + #[inline(always)]"),
    ("fail_stage84_align_notpow2", "power of two",
     "a non-power-of-two #[align]"),
    ("fail_stage84_align_dup", "appears more than once",
     "two #[align] on one fn"),
    ("fail_stage84_link_script_missing", "cannot read the linker script",
     "#![link_script] naming a missing file"),
    ("fail_stage84_link_script_nosections", "no SECTIONS block",
     "#![link_script] naming a script with no SECTIONS"),
    ("fail_stage84_section_unplaced", "is not placed by the linker script",
     "a #[section] the script never places"),
]


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


def hlc_run(src, extra=()):
    """Compile + link + run a Halis program; return (exit_code, output)."""
    c = os.path.join(TMP, os.path.basename(src) + ".c")
    exe = os.path.join(TMP, os.path.basename(src) + ".bin")
    r = run(["./bin/hlc", src, c] + list(extra))
    if r.returncode != 0:
        return None, (r.stdout + r.stderr)
    g = run(["gcc", "-O2", "-o", exe, c, "-lm", "-pthread"])
    if g.returncode != 0:
        return None, g.stderr
    r = run([exe])
    return r.returncode, r.stdout + r.stderr


# ---------------------------------------------------------------------------
print("=== 1. resolution + guards ===")
# ---------------------------------------------------------------------------
if os.path.exists(CORE):
    ok("link: core/section.hls present")
else:
    bad("link: core/section.hls missing")

# A standalone parse of the entry file must not lose the new attributes.
probe = os.path.join(TMP, "probe.hls")
with open(probe, "w", encoding="utf-8") as fh:
    fh.write(
        'import "core.section"\n\n'
        '#[section(".text.probe")]\n'
        '#[align(64)]\n'
        'fn stub(x: int) -> int pure {\n    return x\n}\n\n'
        'fn main() -> int {\n    return stub(1)\n}\n'
    )
# `--check` / compile-to-C only: this probe is about the PARSE, and
# running it would exit with main's return value.
for label, cmd in (("boot", ["python3", "boot/boot.py", "--check", probe]),
                   ("hlc", ["./bin/hlc", probe, os.path.join(TMP, "probe.c")])):
    r = run(cmd)
    if r.returncode == 0:
        ok("link: attributes parse standalone (%s)" % label)
    else:
        bad("link: attributes rejected standalone (%s): %s"
            % (label, (r.stdout + r.stderr).strip()[:120]))

# `#![link_script]` in a dependency is rejected (crate attributes are an
# entry-file property, like `#![freestanding]`).
dep = os.path.join(TMP, "dep.hls")
with open(dep, "w", encoding="utf-8") as fh:
    fh.write('#![link_script("place.ld")]\n\nfn helper() -> int pure {\n    return 1\n}\n')
entry = os.path.join(TMP, "entry_dep.hls")
with open(entry, "w", encoding="utf-8") as fh:
    fh.write('import "./dep.hls"\n\nfn main() -> int {\n    return helper()\n}\n')
r = run(["python3", "boot/boot.py", "--check", entry])
if r.returncode == 1 and "only allowed in the entry file" in (r.stdout + r.stderr):
    ok("link: #![link_script] is entry-file-only")
else:
    bad("link: #![link_script] in a dependency not rejected: %s"
        % (r.stdout + r.stderr).strip()[:120])

# A missing linker script is an error, not a warning.
r = run(["python3", "boot/boot.py", "--check", "tests/fail/fail_stage84_link_script_missing.hls"])
if r.returncode == 1 and "cannot read the linker script" in (r.stdout + r.stderr):
    ok("link: a missing linker script is rejected by name")
else:
    bad("link: missing linker script not reported: %s" % (r.stdout + r.stderr)[:120])

# ---------------------------------------------------------------------------
print("=== 2. standalone parse (core/section.hls) ===")
# ---------------------------------------------------------------------------
src = open(CORE, encoding="utf-8").read()
for enum in ("SectionFault", "SectionKind", "LayoutFault", "ScriptFault"):
    if re.search(r"enum\s+%s\b" % enum, src):
        ok("link: core.section declares enum %s" % enum)
    else:
        bad("link: core.section missing enum %s" % enum)
for struct in ("OutputSection", "PlacedSection", "ImageLayout"):
    if re.search(r"struct\s+%s\b" % struct, src):
        ok("link: core.section declares struct %s" % struct)
    else:
        bad("link: core.section missing struct %s" % struct)
imports = re.findall(r'^import\s+"([^"]+)"', src, re.M)
if imports == ["core.option", "core.result"]:
    ok("link: core.section imports only core.option + core.result (freestanding-safe)")
else:
    bad("link: core.section imports %r" % imports)
if "uses " not in src and "extern " not in src:
    ok("link: core.section declares no effects and no externs")
else:
    bad("link: core.section declares effects or externs")
for fn in ("section_check", "section_check_why", "section_align_ok",
           "section_kind", "parse_link_script", "script_placed_patterns",
           "script_covers", "layout_image", "layout_render",
           "layout_contains", "layout_file_bytes", "layout_load_end"):
    if re.search(r"fn\s+%s\b" % fn, src):
        ok("link: core.section exposes %s" % fn)
    else:
        bad("link: core.section missing %s" % fn)

# ---------------------------------------------------------------------------
print("=== 3. Stage-0 enforcement ===")
# ---------------------------------------------------------------------------
r = run(["python3", "boot/boot.py", OK_TEST])
if r.returncode == 0:
    ok("link: ok-test clean on the interpreter")
else:
    bad("link: ok-test interpreter exit=%d" % r.returncode)

code, out = hlc_run(DEMO)
if code == 0 and "DEMO OK" in out:
    ok("link: examples/section_demo.hls runs natively, DEMO OK")
else:
    bad("link: section_demo exit=%s (%s)" % (code, out.strip()[:120]))

r = run(["python3", "boot/boot.py", DEMO])
if r.returncode == 0 and "DEMO OK" in r.stdout:
    ok("link: section_demo runs on the interpreter, DEMO OK")
else:
    bad("link: section_demo interpreter exit=%d" % r.returncode)

for name, needle, desc in FAIL_PROGRAMS:
    path = "tests/fail/%s.hls" % name
    if not os.path.exists(path):
        bad("link: %s missing" % path)
        continue
    rb = run(["python3", "boot/boot.py", "--check", path])
    if rb.returncode != 1:
        bad("link: %s not rejected by boot" % name)
        continue
    if needle not in (rb.stdout + rb.stderr):
        bad("link: %s rejected without its diagnostic (%s)" % (name, desc))
        continue
    rh = run(["./bin/hlc", "--audit", path])
    if rh.returncode == 0:
        bad("link: %s accepted by hlc (parity)" % name)
        continue
    if needle not in (rh.stdout + rh.stderr):
        bad("link: %s hlc diagnostic differs (parity)" % name)
        continue
    ok("link: %s rejected by both compilers (%s)" % (name, desc))

# ---------------------------------------------------------------------------
print("=== 4. behaviour probes ===")
# ---------------------------------------------------------------------------
PROBES = [
    # (name, source, expected exit code, description)
    ("validator", '''
import "core.section"
fn main() -> int {
    if section_fault_code(section_check(".text.boot")) != 0 { return 1 }
    if section_fault_code(section_check(".text")) != 6 { return 2 }
    if section_fault_code(section_check("")) != 1 { return 3 }
    if section_fault_code(section_check(".a b")) != 4 { return 4 }
    if section_fault_code(section_check(".a..b")) != 5 { return 5 }
    if section_fault_code(section_check("0a")) != 3 { return 6 }
    return 0
}
''', 0, "the name validator matches the compilers' rules"),

    ("kinds", '''
import "core.section"
fn main() -> int {
    if section_kind_name(section_kind(".text.isr")) != "text" { return 1 }
    if section_kind_name(section_kind(".data.rel.ro")) != "data" { return 2 }
    if section_kind_name(section_kind(".bss")) != "bss" { return 3 }
    if section_kind_writable(section_kind(".text")) { return 4 }
    if !section_kind_executable(section_kind(".text")) { return 5 }
    if section_kind_has_bytes(section_kind(".bss")) { return 6 }
    return 0
}
''', 0, "section kinds drive the page flags"),

    ("reader", '''
import "core.result"
import "core.section"
fn main() -> int {
    let s: str = "SECTIONS { .text : ALIGN(4) { KEEP(*(.text .text.*)) } }"
    let r: Result[list[OutputSection], ScriptFault] = parse_link_script(s)
    if result_is_err(r) { return 1 }
    let secs: list[OutputSection] = result_unwrap(r)
    if secs.len() != 1 { return 2 }
    if secs.get(0).align != 4 { return 3 }
    if secs.get(0).inputs.len() != 2 { return 4 }
    if result_is_ok(parse_link_script("ENTRY(x)")) { return 5 }
    return 0
}
''', 0, "the script reader captures sections, ALIGN and input patterns"),

    ("comments", '''
import "core.section"
fn main() -> int {
    let s: str = "/* SECTIONS { .bogus : { } } */ SECTIONS { .text : { *(.text) } }"
    if script_covers_text(s, ".bogus") != "" { return 1 }
    if script_covers_text(s, ".text") != ".text" { return 2 }
    if ls_strip_comments("a /* b */ c").len() != 4 { return 3 }
    return 0
}
''', 0, "a section named inside a comment is not real"),

    ("coverage", '''
import "core.result"
import "core.section"
fn main() -> int {
    let secs: list[OutputSection] = result_unwrap(parse_link_script("SECTIONS { .text : { *(.text .text.*) } }"))
    if script_covers(secs, ".text.isr") != ".text" { return 1 }
    if script_covers(secs, ".textx") != "" { return 2 }
    if script_covers(secs, ".data") != "" { return 3 }
    if pattern_matches(".text*", ".text.a") != true { return 4 }
    if pattern_matches(".text", ".text.a") != false { return 5 }
    return 0
}
''', 0, "GNU ld prefix matching decides coverage"),

    ("solver", '''
import "core.result"
import "core.section"
fn main() -> int {
    let sizes: map[str, int] = map_new()
    sizes.set(".text", 64)
    sizes.set(".bss", 4096)
    let secs: list[OutputSection] = [
        OutputSection{name: ".text", vma: 0, align: 4, noload: false, inputs: []},
        OutputSection{name: ".bss", vma: 0, align: 4096, noload: true, inputs: []}
    ]
    let l: ImageLayout = result_unwrap(layout_image(1048576, secs, sizes))
    if l.entries.get(0).vma != 1048576 { return 1 }
    if l.entries.get(1).vma != 1052672 { return 2 }
    if layout_file_bytes(l) != 64 { return 3 }
    if layout_load_end(l) != 1048640 { return 4 }
    if !layout_contains(l, 1053000) { return 5 }
    if layout_section_at(l, 1053000) != ".bss" { return 6 }
    if layout_render(l).find("0x0000000000100000") < 0 { return 7 }
    return 0
}
''', 0, "the placement solver honours ALIGN, NOLOAD and the pinned address"),

    ("overlap", '''
import "core.result"
import "core.section"
fn main() -> int {
    let sizes: map[str, int] = map_new()
    sizes.set(".text", 64)
    sizes.set(".data", 64)
    let secs: list[OutputSection] = [
        OutputSection{name: ".text", vma: 1048576, align: 4, noload: false, inputs: []},
        OutputSection{name: ".data", vma: 1048600, align: 4, noload: false, inputs: []}
    ]
    if layout_fault_code(result_unwrap_err(layout_image(0, secs, sizes))) != 1 {
        return 1
    }
    let bad: list[OutputSection] = [
        OutputSection{name: ".text", vma: 0, align: 3, noload: false, inputs: []}
    ]
    if layout_fault_code(result_unwrap_err(layout_image(0, bad, map_new()))) != 3 {
        return 2
    }
    return 0
}
''', 0, "an overlapping or misaligned image is an error, not a wrong answer"),

    ("freestanding_read", '''
#![freestanding]
import "core.result"
import "core.section"
fn main() -> int {
    # A kernel author reads its own script in early boot: the scan must
    # not need a heap larger than the 1-MiB freestanding arena.
    let s: str = "SECTIONS { .text : ALIGN(4) { KEEP(*(.text .text.*)) } .data : { *(.data) } }"
    let secs: list[OutputSection] = result_unwrap(parse_link_script(s))
    if secs.len() != 2 { return 1 }
    if script_covers(secs, ".text.boot") != ".text" { return 2 }
    return 0
}
''', 0, "the reader fits the 1-MiB freestanding arena (no strip-then-scan copy)"),
]

for name, body, want, desc in PROBES:
    path = os.path.join(TMP, "probe_%s.hls" % name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    r = run(["python3", "boot/boot.py", path])
    if r.returncode != want:
        bad("link: probe %s (interp) exit=%d want=%d — %s"
            % (name, r.returncode, want, (r.stdout + r.stderr).strip()[:100]))
        continue
    if "#![freestanding]" in body:
        # A freestanding TU has no libc `main`; linking it against libc
        # is the wrong check. The -nostdlib link below is the real one.
        ok("link: %s — %s" % (name, desc))
        continue
    code, out = hlc_run(path)
    if code != want:
        bad("link: probe %s (native) exit=%s want=%d — %s"
            % (name, code, want, out.strip()[:100]))
        continue
    ok("link: %s — %s" % (name, desc))

# The freestanding probe must also LINK -nostdlib (that is what makes
# the arena claim meaningful).
fs_probe = os.path.join(TMP, "probe_freestanding_read.hls")
if os.path.exists(fs_probe):
    c = os.path.join(TMP, "fsprobe.c")
    exe = os.path.join(TMP, "fsprobe")
    r = run(["./bin/hlc", fs_probe, c])
    if r.returncode == 0 and "void _start(void)" in open(c, encoding="utf-8", errors="replace").read():
        g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
                 "-fno-stack-protector", "-Wl,--gc-sections", "-o", exe, c])
        if g.returncode == 0:
            r = run([exe])
            if r.returncode == 0:
                ok("link: the freestanding reader links -nostdlib and exits 0")
            else:
                bad("link: freestanding reader exit=%d" % r.returncode)
        else:
            bad("link: freestanding reader link failed: %s" % g.stderr.strip()[:120])
    else:
        bad("link: freestanding reader emitted no _start")

# ---------------------------------------------------------------------------
print("=== 5. self-hosted emission + parity ===")
# ---------------------------------------------------------------------------
demo_c = os.path.join(TMP, "demo.c")
if run(["./bin/hlc", DEMO, demo_c]).returncode == 0:
    text = open(demo_c, encoding="utf-8", errors="replace").read()
    # The demo does not itself annotate functions; a dedicated program
    # does, so the attributes are checked on real emission.
    sec_src = os.path.join(TMP, "emit.hls")
    with open(sec_src, "w", encoding="utf-8") as fh:
        fh.write(
            '#[section(".text.emit_a")]\n'
            '#[align(64)]\n'
            'fn emit_a(x: int) -> int pure {\n    return x + 1\n}\n\n'
            '#[section(".text.emit_b")]\n'
            'fn emit_b(x: int) -> int pure {\n    return x - 1\n}\n\n'
            'fn main() -> int {\n    return emit_a(2) + emit_b(1)\n}\n'
        )
    emit_c = os.path.join(TMP, "emit.c")
    r = run(["./bin/hlc", sec_src, emit_c])
    if r.returncode != 0:
        bad("link: annotated program did not compile: %s" % (r.stdout + r.stderr)[:120])
    else:
        text = open(emit_c, encoding="utf-8", errors="replace").read()
        if '__attribute__((section(".text.emit_a"), used))' in text:
            ok("link: C carries __attribute__((section(...), used))")
        else:
            bad("link: section attribute missing from the C")
        if "__attribute__((aligned(64)))" in text:
            ok("link: C carries __attribute__((aligned(64)))")
        else:
            bad("link: aligned attribute missing from the C")
        # `used` is load-bearing under --gc-sections.
        if text.count('section(".text.emit_a"), used') >= 2:
            ok("link: the attribute is on both the prototype and the definition")
        else:
            bad("link: section attribute appears on only one of prototype/definition")
        g = run(["gcc", "-O2", "-Werror", "-o", os.path.join(TMP, "emit"), emit_c, "-lm", "-pthread"])
        if g.returncode == 0:
            r = run([os.path.join(TMP, "emit")])
            if r.returncode == 3:
                ok("link: annotated program runs natively (3 + 1 - 1)")
            else:
                bad("link: annotated program exit=%d want 3" % r.returncode)
        else:
            bad("link: annotated program link failed: %s" % g.stderr.strip()[:120])

# The ok-test must link -nostdlib.
fs_ok = os.path.join(TMP, "ok_fs.hls")
with open(fs_ok, "w", encoding="utf-8") as fh:
    # Replace the ATTRIBUTE, not the mention of it in the header comment.
    text = open(OK_TEST, encoding="utf-8").read()
    text = re.sub(r"(?m)^#!\[no_std\]$", "#![freestanding]", text, count=1)
    fh.write(text)
fs_c = os.path.join(TMP, "ok_fs.c")
if run(["./bin/hlc", fs_ok, fs_c]).returncode == 0:
    if "void _start(void)" in open(fs_c, encoding="utf-8", errors="replace").read():
        ok("link: ok-test emits a freestanding TU (_start)")
        g = run(["gcc", "-O2", "-ffreestanding", "-nostdlib", "-ffunction-sections",
                 "-fno-stack-protector", "-Wl,--gc-sections", "-o",
                 os.path.join(TMP, "ok_fs"), fs_c])
        if g.returncode == 0:
            r = run([os.path.join(TMP, "ok_fs")])
            if r.returncode == 0:
                ok("link: ok-test links -nostdlib and exits 0 (freestanding)")
            else:
                bad("link: freestanding ok-test exit=%d" % r.returncode)
        else:
            bad("link: freestanding ok-test link failed: %s" % g.stderr.strip()[:120])
    else:
        bad("link: freestanding ok-test emitted no _start")
else:
    bad("link: ok-test did not compile as freestanding")

# #![link_script] must be ACCEPTED when the coverage holds, and the
# compiled program must still run (a false positive here would make
# custom sections unusable).
good = os.path.join(TMP, "good.hls")
with open(good, "w", encoding="utf-8") as fh:
    fh.write('#![link_script("%s")]\n\n' % os.path.join(ROOT, SCRIPT) + '\n'
             '#[section(".text.boot_stub")]\n'
             'fn boot_stub(x: int) -> int pure {\n    return x * 2\n}\n\n'
             '#[section(".device")]\n'
             'fn dev(x: int) -> int pure {\n    return x + 1\n}\n\n'
             'fn main() -> int {\n    return boot_stub(3) + dev(1)\n}\n')
code, out = hlc_run(good)
if code == 8:
    ok("link: a covered #[section] compiles, links and runs under its script")
else:
    bad("link: covered #[section] exit=%s (%s)" % (code, out.strip()[:120]))

# The shipped link.ld must place what the repo's own demo annotates.
linkld = os.path.join(TMP, "linkld.hls")
with open(linkld, "w", encoding="utf-8") as fh:
    fh.write('#![link_script("%s")]\n\n' % os.path.join(ROOT, "link.ld") + '\n'
             '#[section(".text.demo_stub")]\n'
             'fn demo_stub(x: int) -> int pure {\n    return x\n}\n\n'
             'fn main() -> int {\n    return demo_stub(0)\n}\n')
code, out = hlc_run(linkld)
if code == 0:
    ok("link: the shipped link.ld places a custom section")
else:
    bad("link: shipped link.ld run exit=%s (%s)" % (code, out.strip()[:120]))

# ---------------------------------------------------------------------------
print("=== 6. hlfmt + linter ===")
# ---------------------------------------------------------------------------
fmt_src = os.path.join(TMP, "fmt.hls")
FORMATTED = ('#[section(".text.fmt")]\n#[align(64)]\nfn f(x: int) -> int {\n'
             '    return x\n}\n\nfn main() -> int {\n    return f(1)\n}\n')
with open(fmt_src, "w", encoding="utf-8") as fh:
    fh.write(FORMATTED)
r = run(["python3", "tools/hlfmt.py", "-c", fmt_src])
if r.returncode == 0:
    ok("link: hlfmt -c accepts the new attributes")
else:
    bad("link: hlfmt -c rejected: %s" % (r.stdout + r.stderr).strip()[:120])
# An UNformatted file carrying both attributes must format to something
# that still carries them, and be stable.
with open(fmt_src, "w", encoding="utf-8") as fh:
    fh.write('#[section(".text.fmt")]\n#[align(64)]\nfn f(x:int)->int{\nreturn x}\n\nfn main() -> int {\nreturn f(1)}\n')
r = run(["python3", "tools/hlfmt.py", "-w", fmt_src])
once = open(fmt_src, encoding="utf-8").read()
r = run(["python3", "tools/hlfmt.py", "-w", fmt_src])
twice = open(fmt_src, encoding="utf-8").read()
if once == twice and '#[section(".text.fmt")]' in once and "#[align(64)]" in once:
    ok("link: hlfmt -w is stable and preserves both attributes")
else:
    bad("link: hlfmt -w unstable or lossy:\n%s" % twice[:200])
r = run(["python3", "tools/hllint.py", OK_TEST])
if r.returncode == 0:
    ok("link: hllint clean on the ok-test")
else:
    bad("link: hllint: %s" % (r.stdout + r.stderr).strip()[:160])

# ---------------------------------------------------------------------------
print("=== 7. --audit ===")
# ---------------------------------------------------------------------------
for label, cmd in (("boot", ["python3", "boot/boot.py", "--audit", good]),
                   ("hlc", ["./bin/hlc", "--audit", good])):
    r = run(cmd)
    text = r.stdout + r.stderr
    if r.returncode == 0 and "Linker script:" in text and "place.ld" in text:
        if "section(s) named" in text:
            ok("link: --audit reports the script and the section count (%s)" % label)
        else:
            bad("link: --audit missing the section count (%s)" % label)
    else:
        bad("link: --audit did not report the linker script (%s): %s"
            % (label, text.strip()[:140]))

r = run(["./bin/hlc", "--opt-stats", good, os.path.join(TMP, "stats.c")])
text = r.stdout + r.stderr
if r.returncode == 0 and "#[section(\"X\")]" in text and "#[align(N)]" in text:
    ok("link: --opt-stats counts the section and align annotations")
else:
    bad("link: --opt-stats missing the new counters: %s" % text.strip()[:160])

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print("=" * 70)
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
