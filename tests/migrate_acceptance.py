#!/usr/bin/env python3
"""Stage 129 acceptance gate — hls-migrate, the migration tool.

Run with `make migrate-acceptance` (or
`python3 tests/migrate_acceptance.py`).

Eight sections over the REAL codemod CLI (subprocess, the same
interface a user drives), the REAL migration bed (tests/migrate_tree,
a v0.34-era tree carrying every pre-migration pattern), and the
tool's own arithmetic (imported for the unit laws):

  1. the convention  — usage gates are exit 2 (no PATH, malformed
     & the refusals    versions, a backwards walk, unknown rule ids,
                       non-.hls files, missing paths, an empty
                       directory); --rules lists the six real rules
                       with their real since-versions; --version

  2. the registry    — the oldest-first order; the release-tag
     & the laws        grammar (3-part+suffix, the 2-part v1.0, junk
                       refused); the version-sort law (9 < 10 < 100);
                       the five libxxhash 0.8.3 XXH64 vectors the
                       xxh64-spec rule recomputes against; the hex64
                       word law (minimal-digit, negative and int64
                       minimum forms); the narrow-GPR map's
                       completeness (every 8/16/32-bit spelling
                       migrates to its 64-bit name); the string
                       decoder's escape set (exactly \n \t \\ \")

  3. the plan        — the FULL-RANGE plan over the untouched bed:
     over the bed      exit 1, 18 findings (17 auto + 1 manual)
                       across exactly 4 files, the per-file rule
                       census, the precise line:col of named
                       findings — and the bed is byte-identical
                       afterwards (a plan never touches a file)

  4. the walk        — --apply --verify: exit 0, 17 edits across 3
                       files, all three rewritten files verify OK
                       through the real checker; the .bak beside
                       each rewritten file is byte-identical to the
                       original (the first backup wins);
                       legacy/old_io.hls is untouched and .bak-less
                       (its '..' import is MANUAL advice); the
                       migrated app and digest programs RUN on the
                       interpreter and print the Halis name; the
                       migrated kernel checks clean; the native
                       differential over the runnable pair when
                       bin/hlc exists

  5. the version     — the roadmap phrase verbatim
     gate              (`--from v0.34.0-alpha --to v1.0`) fires
                       exactly the four post-v0.34 rules and gates
                       the two pre-v0.34 dialects off; a v0.110
                       dialect and the since==from boundary are
                       CLEAN (the gate is real, not noise);
                       --only/--skip compose with the gate

  6. the idempotence — the applied tree re-plans with exactly the
     & the honesty     one MANUAL finding (0 auto — the walk is a
                       fixed point); a re-apply writes 0 edits and
                       keeps the first .bak; --diff emits unified
                       diffs; --json carries the census and the
                       gated rule selection; a lexer-invalid escape
                       golden is MANUAL advice, never rewritten;
                       the mirrored golden-first comparison form
                       migrates too

  7. the verify      — --apply --verify fails loudly (exit 1) when a
     gate              migrated file still fails the checker, naming
                       the file and the first error line; a
                       rewritten file that still carries an
                       unresolved MANUAL finding is SKIPPED, named,
                       and the apply still succeeds

  8. the corpus      — tests/ok/feat_stage129_migrate.hls is
     hygiene            hlfmt-canonical, hllint-clean, boots through
                       the real checker, runs green on the
                       interpreter and (when bin/hlc exists) matches
                       natively byte for byte; the pristine bed
                       still fails the checker in exactly the
                       pre-migration places (the absolute import,
                       the 32-bit register, the '..' segment); no
                       .bak litter survives anywhere

Exit code 0 = all acceptance criteria met.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

TOOL = [sys.executable, "tools/hls-migrate.py"]
BOOT = [sys.executable, "boot/boot.py"]
FMT = [sys.executable, "tools/hlfmt.py"]
LINT = [sys.executable, "tools/hllint.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

BED = os.path.join(ROOT, "tests", "migrate_tree")
SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage129_migrate.hls")
TMP = tempfile.mkdtemp(prefix="hls-migrate-gate-")

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print("  ok: " + msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  FAIL: " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=300,
                          text=True, **kw)


def pristine():
    """A byte-exact copy of the untouched migration bed."""
    dst = tempfile.mkdtemp(prefix="hls-migrate-pristine-")
    out = os.path.join(dst, "tree")
    shutil.copytree(BED, out)
    return out


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "hls_migrate", os.path.join(ROOT, "tools", "hls-migrate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def native_recipe(hls_path, out_bin):
    """bin/hlc -> C, gcc -O2, run: the house recipe."""
    c_path = out_bin + ".c"
    r = run([HLC, hls_path, c_path])
    if r.returncode != 0:
        return None, "hlc failed: " + r.stdout.strip()[:120]
    r = run([CC, "-O2", "-o", out_bin, c_path, "-lm", "-pthread"])
    if r.returncode != 0:
        return None, "gcc failed: " + r.stdout.strip()[:120]
    return run([out_bin]), None


# ===========================================================================
def _section_1():
    print("Section 1 — the convention and the refusals")

    p = run(TOOL)
    check(p.returncode == 2 and "no PATH given" in p.stdout,
          "no PATH is exit 2, named")

    p = run(TOOL + [TMP, "--from", "garbage"])
    check(p.returncode == 2
          and "must be a release tag like v0.34.0-alpha" in p.stdout,
          "a malformed --from is exit 2, named")

    p = run(TOOL + [TMP, "--to", "not-a-tag"])
    check(p.returncode == 2 and "--to must be a release tag" in p.stdout,
          "a malformed --to is exit 2, named")

    p = run(TOOL + [TMP, "--from", "v1.0", "--to", "v0.1.0-alpha"])
    check(p.returncode == 2 and "walks forward" in p.stdout,
          "a backwards walk is exit 2, named")

    p = run(TOOL + [TMP, "--only", "nosuch"])
    check(p.returncode == 2 and "unknown rule id: nosuch" in p.stdout,
          "an unknown --only id is exit 2, named")

    p = run(TOOL + [TMP, "--skip", "nosuch"])
    check(p.returncode == 2 and "unknown rule id: nosuch" in p.stdout,
          "an unknown --skip id is exit 2, named")

    p = run(TOOL + [os.path.join(ROOT, "CHANGELOG.md")])
    check(p.returncode == 2 and "not a .hls file" in p.stdout,
          "a non-.hls file is exit 2, named")

    p = run(TOOL + [os.path.join(ROOT, "no_such_dir_here")])
    check(p.returncode == 2 and "no such file or directory" in p.stdout,
          "a missing path is exit 2, named")

    empty = os.path.join(TMP, "empty_dir")
    os.makedirs(empty)
    p = run(TOOL + [empty])
    check(p.returncode == 2 and "no .hls files under" in p.stdout,
          "an empty directory is exit 2, named")

    p = run(TOOL + ["--rules"])
    check(p.returncode == 0, "--rules exits 0")
    for rid in ("html-escape-slash", "rename-project", "import-guard",
                "xxh64-spec", "asm-width", "hex64-word"):
        check(rid in p.stdout, "--rules names %s" % rid)
    for since in ("v0.8.0-alpha", "v0.20.0-alpha", "v0.51.1-alpha",
                  "v0.82.0-alpha", "v0.102.0-alpha", "v0.103.0-alpha"):
        check(since in p.stdout, "--rules dates %s" % since)

    p = run(TOOL + ["--version"])
    check(p.returncode == 0 and "0.147.0-alpha" in p.stdout,
          "--version says 0.147.0-alpha")


# ===========================================================================
def _section_2():
    print("Section 2 — the registry and the unit laws")
    mod = load_tool()

    check([r["id"] for r in mod.RULES] ==
          ["html-escape-slash", "rename-project", "import-guard",
           "xxh64-spec", "asm-width", "hex64-word"],
          "the registry is oldest-first across the six real rules")

    check(mod.parse_version("v0.34.0-alpha") == (0, 34, 0),
          "parse_version: 3-part + suffix")
    check(mod.parse_version("v1.0") == (1, 0, 0),
          "parse_version: the 2-part v1.0")
    check(mod.parse_version("v0.51.1-alpha") == (0, 51, 1),
          "parse_version: the patch level")
    check(mod.parse_version("garbage") is None
          and mod.parse_version("0.34") is None,
          "parse_version: junk is refused")

    check(mod.parse_version("v0.9.0-alpha") < mod.parse_version("v0.10.0-alpha")
          < mod.parse_version("v0.100.0-alpha"),
          "the version-sort law: 9 < 10 < 100, never alphabetical")

    # The five libxxhash-0.8.3 vectors (seed 0) — the same values
    # tests/ok/feat_stage43_hash.hls has pinned since the spec fix.
    vectors = [
        (b"", -1205034819632174695),
        (b"a", -3292477735350538661),
        (b"hello", 2794345569481354659),
        (b"a" * 32, -8831976466377237801),
        (b"a" * 64, -1379396020616355102),
    ]
    for data, want in vectors:
        check(mod.to_signed64(mod.xxh64(data, 0)) == want,
              "XXH64(%r...) matches libxxhash 0.8.3" % data[:6])

    check(mod.hex64_render(0) == "0x0000000000000000"
          and mod.hex64_render(255) == "0x00000000000000ff"
          and mod.hex64_render(-1) == "0xffffffffffffffff"
          and mod.hex64_render(-9223372036854775808) == "0x8000000000000000"
          and mod.hex64_render(9223372036854775807)
          == "0x7fffffffffffffff",
          "the hex64 word law: minimal, negative and boundary words")

    m = mod._NARROW_TO_64
    check(len(m) == 52 and len(set(m.values())) == 16,
          "the narrow-GPR map: 52 narrow spellings, 16 destinations")
    check(m["eax"] == "rax" and m["r8d"] == "r8" and m["dx"] == "rdx"
          and m["al"] == "rax" and m["ah"] == "rax" and m["r14d"] == "r14"
          and m["spl"] == "rsp" and m["r9b"] == "r9",
          "the narrow-GPR map: every family lands on its 64-bit name")
    check("rax" not in m and "r9" not in m,
          "the narrow-GPR map: 64-bit names are not their own source")

    check(mod.decode_str_literal('"a\\n\\t\\\\\\"b"') == b"a\n\t\\\"b",
          "the string decoder: exactly the lexer's escape set")
    check(mod.decode_str_literal('"a\\q"') is None,
          "the string decoder: an unknown escape is refused, not guessed")

    check(mod.html_escape_today(b"</b>") == b"&lt;&#x2F;b&gt;",
          "html_escape_today: the forward slash is escaped")
    check(mod.html_escape_today(b"a&b\"c'd<e>f")
          == b"a&amp;b&quot;c&#39;d&lt;e&gt;f",
          "html_escape_today: the full entity set, byte-wise")


# ===========================================================================
def _section_3():
    print("Section 3 — the plan over the real bed")
    keep = pristine()  # byte-exact reference for the no-touch law

    p = run(TOOL + [BED])
    check(p.returncode == 1, "a plan with findings is exit 1")
    out = p.stdout
    check("18 findings across 4 files (17 auto, 1 manual)" in out,
          "the census: 18 findings (17 auto, 1 manual) across 4 files")
    check(out.count("[rename-project] AUTO") == 3
          and out.count("[html-escape-slash] AUTO") == 2
          and out.count("[hex64-word] AUTO") == 2
          and out.count("[xxh64-spec] AUTO") == 1
          and out.count("[import-guard] AUTO") == 2
          and out.count("[asm-width] AUTO") == 7
          and out.count("[import-guard] MANUAL") == 1,
          "the per-rule census: 3+2+2+2+1+7 auto and 1 manual")

    for needle in ("app.hls:26:8: [import-guard] AUTO",
                   "app.hls:11:19: [hex64-word] AUTO",
                   "digest.hls:16:31: [xxh64-spec] AUTO",
                   "kernel.hls:17:24: [asm-width] AUTO",
                   "legacy/old_io.hls:12:8: [import-guard] MANUAL"):
        check(needle in out, "the plan names %s" % needle)

    check("must not contain '..'" in out
          or "never auto-rewritten" in out,
          "the MANUAL finding carries the guard's reason")

    # The no-touch law: a plan is read-only, byte for byte.
    untouched = True
    for root, _dirs, names in os.walk(keep):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), keep)
            a = open(os.path.join(root, name), "rb").read()
            b = open(os.path.join(BED, rel), "rb").read()
            if a != b:
                untouched = False
    check(untouched, "a plan never touches a file (byte-identical bed)")
    shutil.rmtree(os.path.dirname(keep), ignore_errors=True)


# ===========================================================================
def _section_4():
    print("Section 4 — the walk: apply, verify, backups, the run")
    tree = os.path.join(TMP, "walk")
    shutil.copytree(BED, tree)
    pre = {}
    for root, _d, names in os.walk(tree):
        for name in names:
            p = os.path.join(root, name)
            pre[os.path.relpath(p, tree)] = open(p, "rb").read()

    p = run(TOOL + [tree, "--apply", "--verify"])
    out = p.stdout
    check(p.returncode == 0, "--apply --verify exits 0")
    check("applied 17 edits across 3 files" in out,
          "the apply summary: 17 edits across 3 files")
    check(out.count("verify OK") == 3,
          "all three rewritten files verify OK through the checker")
    check("MANUAL work remains" in out and "old_io.hls" in out,
          "the remaining MANUAL work is named")

    # The .bak discipline: byte-identical to the original, first-wins.
    baks_ok = True
    for rel in ("app.hls", "digest.hls", "kernel.hls"):
        bak = os.path.join(tree, rel + ".bak")
        if not os.path.isfile(bak) or open(bak, "rb").read() != pre[rel]:
            baks_ok = False
    check(baks_ok, "each rewritten file carries a .bak of the original")

    check(not os.path.exists(os.path.join(tree, "legacy", "old_io.hls.bak"))
          and open(os.path.join(tree, "legacy", "old_io.hls"), "rb").read()
          == pre[os.path.join("legacy", "old_io.hls")],
          "legacy/old_io.hls is untouched and .bak-less (MANUAL advice)")

    # The migrated content, spot-read. (The header comment still names
    # the OLD import as documentation — the ACTIVE import line is what
    # migrated, so the assertions anchor on the line start.)
    app = open(os.path.join(tree, "app.hls")).read()
    check('\nimport "./helper.hls"' in app
          and '\nimport "/helper.hls"' not in app,
          "the absolute import migrated file-relative")
    check('"Halis is a programming language"' in app
          and "Hieu Louis" not in app,
          "the project name migrated")
    check('hex64(255) != "0x00000000000000ff"' in app,
          "the hex64 golden migrated to the full word")
    check('html_escape("</b>") != "&lt;&#x2F;b&gt;"' in app,
          "the html golden migrated to the escaped slash")
    k = open(os.path.join(tree, "kernel.hls")).read()
    check('out("eax")' not in k and 'out("edx")' not in k
          and 'in("dx")' not in k and 'out("al")' not in k
          and 'inout("ecx")' not in k and 'clobber("r9d")' not in k
          and 'out("r14d")' not in k,
          "no narrow GPR spelling survives the asm-width walk")
    check('out("rax")' in k and 'out("rdx")' in k and 'in("rdx")' in k
          and 'inout("rcx")' in k and 'clobber("r9")' in k
          and 'out("r14")' in k,
          "the 64-bit spellings are in place")

    # The migrated programs RUN on the interpreter.
    p = run(BOOT + [os.path.join(tree, "app.hls")])
    check(p.returncode == 0 and "Halis is a programming language" in p.stdout
          and "app migrated OK" in p.stdout,
          "the migrated app runs green on the interpreter, Halis named")
    p = run(BOOT + [os.path.join(tree, "digest.hls")])
    check(p.returncode == 0 and "made by Halis" in p.stdout
          and "digest migrated OK" in p.stdout,
          "the migrated digest runs green, the recomputed digest matches")
    p = run(BOOT + ["--check", os.path.join(tree, "kernel.hls")])
    check(p.returncode == 0 and "OK: types and effects valid" in p.stdout,
          "the migrated kernel checks clean")

    # The native differential over the runnable pair (house recipe).
    if NATIVE:
        same = True
        for name in ("app", "digest"):
            hls = os.path.join(tree, name + ".hls")
            p = run(BOOT + [hls])
            nbin = os.path.join(TMP, "s4_" + name)
            r, err = native_recipe(hls, nbin)
            if r is None:
                same = False
                bad("  native %s: %s" % (name, err))
            elif (r.returncode, r.stdout) != (p.returncode, p.stdout):
                same = False
                bad("  native divergence on %s" % name)
        check(same, "the migrated pair: interpreter == native, byte for byte")
    else:
        print("  (bin/hlc absent — the native differential is skipped, "
              "named, not silent)")


# ===========================================================================
def _section_5():
    print("Section 5 — the version gate")
    tree = os.path.join(TMP, "gate34")
    shutil.copytree(BED, tree)

    # The roadmap phrase, verbatim: hls migrate v0.34 -> v1.0.
    p = run(TOOL + [tree, "--from", "v0.34.0-alpha", "--to", "v1.0"])
    out = p.stdout
    check(p.returncode == 1, "the phrase's plan is exit 1 (work remains)")
    check("[rename-project]" not in out and "[html-escape-slash]" not in out,
          "the two pre-v0.34 dialects are gated off")
    check(out.count("[import-guard] AUTO") == 2
          and out.count("[xxh64-spec] AUTO") == 1
          and out.count("[asm-width] AUTO") == 7
          and out.count("[hex64-word] AUTO") == 2
          and out.count("[import-guard] MANUAL") == 1,
          "exactly the four post-v0.34 rules fire (2+1+7+2 auto, 1 manual)")
    check("13 findings across 4 files (12 auto, 1 manual)" in out,
          "the gated census: 13 findings (12 auto, 1 manual)")

    # A v0.110 dialect: everything is behind it — CLEAN, exit 0.
    tree110 = os.path.join(TMP, "gate110")
    shutil.copytree(BED, tree110)
    p = run(TOOL + [tree110, "--from", "v0.110.0-alpha"])
    check(p.returncode == 0 and "CLEAN" in p.stdout,
          "a v0.110 dialect is CLEAN: the gate is real, not noise")

    # The since==from boundary: code written AT the breaking release
    # already conforms — the rule does not fire.
    tree103 = os.path.join(TMP, "gate103")
    shutil.copytree(BED, tree103)
    p = run(TOOL + [tree103, "--from", "v0.103.0-alpha"])
    check(p.returncode == 0 and "CLEAN" in p.stdout,
          "the since==from boundary: the rule is gated at its own release")

    # One release earlier and exactly the newest rule fires.
    tree102 = os.path.join(TMP, "gate102")
    shutil.copytree(BED, tree102)
    p = run(TOOL + [tree102, "--from", "v0.102.0-alpha"])
    check(p.returncode == 1
          and p.stdout.count("[hex64-word] AUTO") == 2
          and p.stdout.count("[asm-width]") == 0,
          "one release earlier: exactly hex64-word fires")

    # --only / --skip compose with the gate.
    p = run(TOOL + [tree, "--from", "v0.34.0-alpha", "--only", "asm-width"])
    check(p.returncode == 1 and p.stdout.count("[asm-width] AUTO") == 7
          and "[hex64-word]" not in p.stdout,
          "--only asm-width selects the register walk alone")
    p = run(TOOL + [tree, "--from", "v0.34.0-alpha", "--skip",
                    "asm-width,import-guard"])
    check(p.returncode == 1 and "[asm-width]" not in p.stdout
          and "[import-guard]" not in p.stdout
          and "[hex64-word] AUTO" in p.stdout,
          "--skip composes: the excluded rules never fire")

    # The json carries the gated selection.
    jpath = os.path.join(TMP, "gate.json")
    p = run(TOOL + [tree, "--from", "v0.34.0-alpha", "--to", "v1.0",
                    "--json", jpath])
    rec = json.load(open(jpath))
    check(rec["rules_selected"] == ["import-guard", "xxh64-spec",
                                    "asm-width", "hex64-word"],
          "the json's rules_selected is the gated four")
    check(rec["summary"]["findings_auto"] == 12
          and rec["summary"]["findings_manual"] == 1,
          "the json summary agrees with the human census")


# ===========================================================================
def _section_6():
    print("Section 6 — the idempotence and the honesty gates")
    tree = os.path.join(TMP, "idem")
    shutil.copytree(BED, tree)
    p = run(TOOL + [tree, "--apply"])
    check(p.returncode == 0, "the first apply succeeds")

    # The fixed point: the re-plan finds exactly the MANUAL finding.
    p = run(TOOL + [tree])
    check(p.returncode == 1
          and "1 finding across 1 file (0 auto, 1 manual)" in p.stdout,
          "the re-plan: exactly the MANUAL finding survives (0 auto)")

    # A re-apply writes nothing and keeps the FIRST backup.
    before = open(os.path.join(tree, "app.hls.bak"), "rb").read()
    p = run(TOOL + [tree, "--apply"])
    check(p.returncode == 0 and "applied 0 edits across 0 files" in p.stdout,
          "the re-apply writes 0 edits")
    check(open(os.path.join(tree, "app.hls.bak"), "rb").read() == before,
          "the first backup wins (not overwritten by the re-apply)")

    # --diff: unified diffs with real headers and real content.
    fresh = os.path.join(TMP, "diffme")
    shutil.copytree(BED, fresh)
    p = run(TOOL + [fresh, "--diff"])
    check(p.returncode == 1, "a diff with work is exit 1")
    check("--- " in p.stdout and "+++ " in p.stdout
          and "@@" in p.stdout,
          "the diff carries unified headers")
    check('-import "/helper.hls"' in p.stdout
          and '+import "./helper.hls"' in p.stdout,
          "the diff shows the import rewrite")
    check('-    let who: str = "Hieu Louis is a programming language"'
          in p.stdout
          and '+    let who: str = "Halis is a programming language"'
          in p.stdout,
          "the diff shows the rename")

    # --json to a file: the census, parseable.
    jpath = os.path.join(TMP, "plan.json")
    p = run(TOOL + [fresh, "--json", jpath])
    check(p.returncode == 1 and os.path.isfile(jpath),
          "--json FILE writes the report")
    rec = json.load(open(jpath))
    check(rec["summary"] == {"files_scanned": 5, "files_changed": 3,
                             "findings_auto": 17, "findings_manual": 1},
          "the json census: 5 scanned, 3 changed, 17+1 findings")
    check(rec["rules_selected"] == ["html-escape-slash", "rename-project",
                                    "import-guard", "xxh64-spec",
                                    "asm-width", "hex64-word"],
          "the default gate selects all six rules")

    # A golden the decoder cannot read is MANUAL advice, never a rewrite.
    synth = os.path.join(TMP, "synth")
    os.makedirs(synth)
    with open(os.path.join(synth, "escapes.hls"), "w") as fh:
        fh.write('# an expectation with an escape the lexer refuses\n'
                 'fn main() -> int {\n'
                 '    if html_escape("a") != "a\\q" {\n'
                 '        return 1\n'
                 '    }\n'
                 '    return 0\n'
                 '}\n')
    p = run(TOOL + [synth])
    check(p.returncode == 1 and "not decodable" in p.stdout
          and "MANUAL" in p.stdout,
          "a lexer-invalid escape golden is MANUAL advice")
    check(open(os.path.join(synth, "escapes.hls")).read()
          .count('"a\\q"') == 1,
          "the undecodable golden is untouched")

    # The mirrored comparison form: golden first, hex64 call second.
    with open(os.path.join(synth, "mirror.hls"), "w") as fh:
        fh.write('# the golden-first form the walk also owns\n'
                 'fn main() -> int {\n'
                 '    if "0xff" == hex64(255) {\n'
                 '        return 1\n'
                 '    }\n'
                 '    return 0\n'
                 '}\n')
    p = run(TOOL + [synth])
    check("mirror.hls:3:8: [hex64-word] AUTO" in p.stdout,
          "the mirrored hex64 comparison is found at its column")
    p = run(TOOL + [synth, "--only", "hex64-word", "--apply"])
    check(p.returncode == 0
          and 'if "0x00000000000000ff" == hex64(255)' in
          open(os.path.join(synth, "mirror.hls")).read(),
          "the mirrored golden migrates in place")


# ===========================================================================
def _section_7():
    print("Section 7 — the verify gate")
    tree = os.path.join(TMP, "verify")
    shutil.copytree(BED, tree)
    # A file the walk rewrites but which still carries a REAL type
    # error: migration fixes the dialect, not the bug.
    with open(os.path.join(tree, "broken.hls"), "w") as fh:
        fh.write('# a v0.34-era file with a bug migration cannot fix\n'
                 'import "/helper.hls"\n'
                 '\n'
                 'fn main() -> int {\n'
                 '    let x: int = "not an int"\n'
                 '    return x\n'
                 '}\n')
    p = run(TOOL + [tree, "--apply", "--verify"])
    out = p.stdout
    check(p.returncode == 1 and "verify FAIL" in out
          and "broken.hls" in out,
          "a migrated file that still fails the checker is exit 1, named")
    check("verification FAILED for 1 file(s)" in out,
          "the verify verdict is loud")

    # A rewritten file with an unresolved MANUAL finding is SKIPPED,
    # named — the tool refuses to certify what it knows is broken.
    tree2 = os.path.join(TMP, "verify2")
    shutil.copytree(BED, tree2)
    with open(os.path.join(tree2, "mixed.hls"), "w") as fh:
        fh.write('# rename fires, the .. import stays MANUAL\n'
                 'import "../helper.hls"\n'
                 '\n'
                 'fn main() -> int {\n'
                 '    let who: str = "Hieu Louis"\n'
                 '    return 0\n'
                 '}\n')
    p = run(TOOL + [tree2, "--apply", "--verify"])
    out = p.stdout
    check(p.returncode == 0 and "verify skipped" in out
          and "mixed.hls" in out and "import-guard" in out,
          "a rewritten MANUAL-carrying file is skipped, named, exit 0")
    check(open(os.path.join(tree2, "mixed.hls")).read()
          .count('import "../helper.hls"') == 1,
          "the '..' import survived the apply untouched")


# ===========================================================================
def _section_8():
    print("Section 8 — the corpus hygiene")
    p = run(FMT + ["--check", SHOWCASE])
    check(p.returncode == 0 and "already formatted" in p.stdout,
          "the ok fixture is hlfmt-canonical")
    p = run(LINT + [SHOWCASE])
    check(p.returncode == 0 and "no warnings" in p.stdout,
          "the ok fixture is hllint-clean")
    p = run(BOOT + ["--check", SHOWCASE])
    check(p.returncode == 0 and "OK: types and effects valid" in p.stdout,
          "the ok fixture boots through the real checker")
    p = run(BOOT + [SHOWCASE])
    check(p.returncode == 0 and "migrate laws OK" in p.stdout,
          "the ok fixture runs green on the interpreter")

    if NATIVE:
        i = run(BOOT + [SHOWCASE])
        n, err = native_recipe(SHOWCASE, os.path.join(TMP, "s8_showcase"))
        check(n is not None and (n.returncode, n.stdout)
              == (i.returncode, i.stdout),
              "the ok fixture: interpreter == native, byte for byte")
    else:
        print("  (bin/hlc absent — the native leg is skipped, named, "
              "not silent)")

    # The pristine bed still fails the checker in exactly the
    # pre-migration places — the reason migration exists.
    p = run(BOOT + ["--check", os.path.join(BED, "app.hls")])
    check(p.returncode != 0 and "must be relative, got absolute" in p.stdout,
          "the pristine bed: the absolute import is rejected")
    p = run(BOOT + ["--check", os.path.join(BED, "kernel.hls")])
    check(p.returncode != 0 and "is 32-bit" in p.stdout,
          "the pristine bed: the narrow register is rejected")
    p = run(BOOT + ["--check", os.path.join(BED, "legacy", "old_io.hls")])
    check(p.returncode != 0 and "'..' segments" in p.stdout,
          "the pristine bed: the '..' segment is rejected")

    # No .bak litter survives anywhere the gate works.
    litter = []
    for root, _dirs, names in os.walk(BED):
        for name in names:
            if name.endswith(".bak"):
                litter.append(os.path.join(root, name))
    check(not litter, "no .bak litter in the pristine bed")


# ---------------------------------------------------------------------------
# Section selection: the whole gate is one process by default; a CI
# step timeout can run it in pieces (--sections 1,2 or the
# HLS_MIGRATE_GATE_SECTIONS env). A subset run reports the sections it
# ran, never a silent skip.
_ALL_SECTIONS = list(range(1, 9))
if "--sections" in sys.argv:
    _want = sys.argv[sys.argv.index("--sections") + 1]
    _selected = [int(x) for x in _want.split(",") if x.strip()]
else:
    _env = os.environ.get("HLS_MIGRATE_GATE_SECTIONS")
    _selected = ([int(x) for x in _env.split(",") if x.strip()]
                 if _env else _ALL_SECTIONS)
for _bad_n in _selected:
    if _bad_n not in _ALL_SECTIONS:
        sys.stderr.write("migrate gate: no such section: %d\n" % _bad_n)
        raise SystemExit(2)
if _selected != _ALL_SECTIONS:
    print("migrate gate: running sections %s of %s"
          % (",".join(str(x) for x in _selected),
             ",".join(str(x) for x in _ALL_SECTIONS)))
for _n in _selected:
    globals()["_section_%d" % _n]()

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
raise SystemExit(0 if FAIL == 0 else 1)
