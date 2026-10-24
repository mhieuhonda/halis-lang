#!/usr/bin/env python3
"""hls-guarantee — the Stage 135 soundness guarantee ledger
(bug-bounty-eligible guarantees, documented AND re-demonstrated).

Usage:
  python3 tools/hls-guarantee.py list   [--family FAM] [--json]
  python3 tools/hls-guarantee.py show <ID>
  python3 tools/hls-guarantee.py verify [--root PATH] [--json] [--quiet]
  python3 tools/hls-guarantee.py report --out FILE.md [--json FILE.json]
                                        [--stamp] [--root PATH]

Roadmap Stage 135 is one phrase: "Bug-bounty-eligible soundness
guarantees documented". SECURITY.md has carried the security model in
prose since v0.30.0-alpha, but prose rots: nothing fails when the code
it describes changes. This stage turns the model into a LEDGER — one
machine-readable table of every soundness guarantee the toolchain
makes, where the document is only allowed to claim what the CURRENT
tree still demonstrates:

  1. an ANCHOR is a (file, pattern) pair that must exist in the live
     tree — the enforcement point the guarantee rests on, grepped at
     run time, with the line number recorded. A deleted or renamed
     enforcement point Voids its guarantee and the tool refuses;
  2. a PROBE is a real program re-run through the REAL toolchain at
     verify time: a static probe must be REFUSED by the checker with
     the pinned message substring, a runtime probe must PANIC with the
     pinned exit code and message. The guarantee is demonstrated, not
     asserted;
  3. the REPORT regenerates the bug-bounty scope document from the
     ledger, anchored to the current commit sha and the ledger
     digest. It is deterministic — sorted walks, no wall-clock unless
     --stamp — so two regenerations at the same tree are
     byte-identical, and any hand-edit is visible as a diff.

A bounty claim maps onto exactly one ledger entry: the reporter names
the guarantee id, ships a reproducer the toolchain ACCEPTS, and
demonstrates the violation the entry's violation contract describes.
Everything the toolchain honestly does NOT guarantee lives in the
out-of-scope register — the same honesty SECURITY.md has always
carried, now versioned with the ledger.

Commands:
  list    the ledger table (id, family, severity, claim). --family
          filters. --json for machines.
  show    one guarantee in full: claim, mechanism, anchors, probes,
          violation contract, reproduction requirements, scope.
  verify  the anti-rot pass. Every anchor grepped in the tree (or the
          tree --root points at), every probe re-run through the real
          boot.py checker/interpreter, every policy anchor (the
          SECURITY.md reporting channel) confirmed. Any miss exits 1
          naming the guarantee. --json emits the machine verdict.
  report  regenerate the scope document. Runs verify first — a report
          is never written from a violated ledger. The markdown embeds
          every guarantee with its LIVE anchor line numbers and probe
          verdicts, the severity vocabulary, the eligibility contract,
          the out-of-scope register and the reporting channel.

Exit codes: 0 verified / 1 a named violation (fail-closed) /
2 usage error. Every refusal names the rule, not just the fact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

_TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_TOOLS)

POLICY = "halis-guarantee-1"
BOOT = os.path.join(ROOT, "tools", "hlinterp", "boot.py")
SECURITY_MD = "SECURITY.md"

FAMILIES = ("types", "memory", "arithmetic", "effects", "taint",
            "concurrency", "match", "capabilities", "sandbox",
            "consttime", "ffi", "determinism")
SEVERITIES = ("critical", "high", "medium")
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2}
SEVERITY_MEANING = {
    "critical":
    "the guarantee's failure means memory unsafety, unchecked world "
    "effects, or silent cross-implementation divergence",
    "high":
    "the guarantee's failure means a security boundary (exhaustive "
    "matching, capability authority, sandbox, constant-time, FFI "
    "honesty) is bypassed without the user's consent",
    "medium":
    "the guarantee's failure means a soundness-adjacent property "
    "(name resolution, corpus agreement) no longer holds",
}

# The policy anchors: the scope document quotes the reporting channel
# from SECURITY.md LIVE — if the channel moves or the disclosure
# window changes, the quote must follow, or the ledger refuses.
POLICY_ANCHORS = [
    (SECURITY_MD, "security/advisories"),
    (SECURITY_MD, "90-day coordinated-disclosure"),
]

# ---------------------------------------------------------------------------
# The ledger. Definition order is the presentation order: the two
# memory guarantees open the table because memory unsafety is the
# bounty's worst class, the meta guarantees (bootstrap determinism,
# corpus agreement) close it because they are guarantees ABOUT the
# guarantees. Every anchor below was grepped at the tree this stage
# shipped from; every probe is a real fixture the suite already runs.
# ---------------------------------------------------------------------------
LEDGER = [
    {
        "id": "G-MEM-01",
        "family": "memory",
        "severity": "critical",
        "claim":
        "A moved or dropped binding is unusable: ownership is checked "
        "statically on every control-flow path.",
        "mechanism":
        "The checker carries a moved-flag per binding and refuses any "
        "later read, including through if/while/for arms, with a "
        "use-of-moved-value error naming the binding.",
        "enforced_by": "tools/hlinterp/checking (the ownership checker) + the "
                       "self-hosted checker in src/hlc.hls",
        "anchors": [
            ("tools/hlinterp/checking/expr.py", "use of moved value"),
            ("tools/hlinterp/checking/stmt.py", "use of moved value"),
        ],
        "probes": [
            {"path": "tests/fail/fail_use_after_drop.hls",
             "expect": "use of moved value", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_take_moved.hls",
             "expect": "use of moved value", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_uaf_if.hls",
             "expect": "use of moved value", "kind": "refuse", "args": []},
        ],
        "violation":
        "A program the checker ACCEPTS that uses a binding after its "
        "ownership was released (drop/take/move), on any path.",
        "repro":
        "One .hls file where a binding is released and later read; the "
        "checker output showing acceptance; the interpreter or native "
        "run exhibiting the use-after-release.",
        "out_of_scope":
        "A user explicitly re-binding with clone() and calling it a "
        "violation; drops the checker correctly proves dead.",
    },
    {
        "id": "G-MEM-02",
        "family": "memory",
        "severity": "critical",
        "claim":
        "There is no manual free: the refcounted runtime releases "
        "exactly at scope exit, so double-free and use-after-free are "
        "structural impossibilities in generated code.",
        "mechanism":
        "The codegen emits exact retain/release pairs (hl_retain/"
        "hl_release) on every control-flow path; the memory-stress "
        "acceptance holds RSS flat under ulimit -v 262144.",
        "enforced_by": "src/hlc codegen (refcount emission) + the C "
                       "runtime + tests/memcheck",
        "anchors": [
            ("src/hlc/codegen.hls", "hl_release"),
            ("src/hlc/runtime.hls", "hl_release"),
        ],
        "probes": [
            {"path": "tests/fail/fail_double_drop.hls",
             "expect": "use of moved value", "kind": "refuse", "args": []},
        ],
        "violation":
        "Generated C that frees a refcounted value twice, or reads one "
        "after its count reached zero, for a program the checker "
        "accepts.",
        "repro":
        "The accepted .hls program, the generated .c, and the native "
        "run under valgrind/ASan showing the double free or UAF.",
        "out_of_scope":
        "User C code reached through extern blocks; allocator exhaustion "
        "(an OOM is a controlled panic, not unsafety).",
    },
    {
        "id": "G-TYPE-01",
        "family": "types",
        "severity": "critical",
        "claim":
        "Every value's type is known statically and no implicit "
        "conversion ever changes it.",
        "mechanism":
        "The checker rejects int-to-float and any cross-primitive "
        "coercion without an explicit conversion call, rejects "
        "mismatched assignments and non-bool conditions, and the lexer "
        "rejects integer literals beyond int64.",
        "enforced_by": "tools/hlinterp/checking (type checker) + tools/hlinterp/lexer",
        "anchors": [
            ("tools/hlinterp/checking/expr.py", "type mismatch"),
            ("tools/hlinterp/checking/expr.py", "integer literal too large"),
        ],
        "probes": [
            {"path": "tests/fail/fail_implicit_cast.hls",
             "expect": "type mismatch", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_type_mismatch.hls",
             "expect": "type mismatch", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_int_overflow_literal.hls",
             "expect": "integer literal too large", "kind": "refuse",
             "args": []},
        ],
        "violation":
        "A program the checker accepts where a value changes type "
        "without an explicit conversion, or where a condition is not "
        "bool.",
        "repro":
        "The accepted .hls file and the checker output; a divergent "
        "interpreter-vs-native run for the same file strengthens it.",
        "out_of_scope":
        "Explicit conversions (to_float, to_str) doing exactly what "
        "SPEC section 7 documents, including their panics.",
    },
    {
        "id": "G-ARITH-01",
        "family": "arithmetic",
        "severity": "critical",
        "claim":
        "Every arithmetic operation is checked: overflow, division by "
        "zero and out-of-bounds access are controlled panics (exit "
        "101), never undefined behaviour.",
        "mechanism":
        "The interpreter raises HLPanic at each checked op; the C "
        "runtime mirrors the checks so native binaries panic with the "
        "same message and code; literals beyond int64 are refused at "
        "compile time.",
        "enforced_by": "tools/hlinterp/interp_parts/rt_num.py + the emitted C "
                       "runtime (HL_CHECK paths)",
        "anchors": [
            ("tools/hlinterp/interp_parts/rt_num.py", "integer overflow"),
            ("tools/hlinterp/interp_parts/rt_num.py", "division by zero"),
        ],
        "probes": [
            {"path": "tests/ok/panic_divzero.hls",
             "expect": "division by zero", "kind": "panic", "args": []},
            {"path": "tests/ok/panic_bounds.hls",
             "expect": "out of bounds", "kind": "panic", "args": []},
            {"path": "tests/ok/panic_overflow.hls",
             "expect": "integer overflow", "kind": "panic", "args": []},
        ],
        "violation":
        "Any accepted program where an overflowing operation, a "
        "division by zero or an out-of-bounds access produces a wrong "
        "value, an interpreter/native divergence, or a signal other "
        "than the controlled panic.",
        "repro":
        "The .hls program, the observed output/exit, and the expected "
        "panic per SPEC section 7.",
        "out_of_scope":
        "Float NaN/inf semantics (SPEC pins them as values, not "
        "panics); wraparound explicitly requested through a documented "
        "wrapping builtin, when one exists.",
    },
    {
        "id": "G-EFF-01",
        "family": "effects",
        "severity": "critical",
        "claim":
        "No function reaches a world effect (IO, Fs, Clock, Args, "
        "Exit, Net, Rand, Proc, Conc) that it does not declare; the "
        "fixpoint runs over the whole static call graph.",
        "mechanism":
        "Effect sets are computed to fixpoint over the call graph and "
        "compared against the declared uses clause; pure functions "
        "must transitively call nothing effectful, with the offending "
        "edge named in the error.",
        "enforced_by": "tools/hlinterp/checking (effect fixpoint) + the audit "
                       "surface (hlc --audit)",
        "anchors": [
            ("tools/hlinterp/checking/match_fx.py", "requires effect"),
            ("tools/hlinterp/checking/match_fx.py", "missing: %s"),
        ],
        "probes": [
            {"path": "tests/fail/fail_effect_io.hls",
             "expect": "requires effect", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_pure_uses_io.hls",
             "expect": "requires effect", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_effect_transitive.hls",
             "expect": "requires effect", "kind": "refuse", "args": []},
        ],
        "violation":
        "A program accepted whose function performs console, file, "
        "clock, network, randomness, subprocess or concurrency work "
        "its declared effect set does not cover.",
        "repro":
        "The accepted .hls file, the checker output, and the observed "
        "world effect (e.g. a file written) from the run.",
        "out_of_scope":
        "Effects a user grants deliberately via uses; the alias law "
        "(uses IO covering the IO family) documented in SPEC 9.",
    },
    {
        "id": "G-TAINT-01",
        "family": "taint",
        "severity": "critical",
        "claim":
        "A tainted value cannot reach a declared sink — builtin or "
        "#[taint_sink] extern — without passing a sanitizer.",
        "mechanism":
        "tainted[T] flows through the type graph to every sink "
        "argument; print/eprint/file/exit/net/proc sinks refuse at "
        "compile time naming the sanitizer options; extern boundaries "
        "declare their own sinks via #[taint_sink(...)].",
        "enforced_by": "tools/hlinterp/checking/call.py (sink enforcement) + "
                       "tools/hlinterp/ffi_taint.py (FFI boundary)",
        "anchors": [
            ("tools/hlinterp/checking/call.py", "taint-sink violation"),
            ("tools/hlinterp/ffi_taint.py", "taint_sink"),
        ],
        "probes": [
            {"path": "tests/fail/fail_taint_print.hls",
             "expect": "taint-sink violation", "kind": "refuse",
             "args": []},
            {"path": "tests/fail/fail_eprint_tainted.hls",
             "expect": "taint-sink violation", "kind": "refuse",
             "args": []},
            {"path": "tests/fail/fail_stage109_ffi_sink.hls",
             "expect": "taint-sink violation", "kind": "refuse",
             "args": []},
        ],
        "violation":
        "A program accepted where a value derived from tainted_args(), "
        "read_file_tainted() or read_line() reaches a sink without a "
        "sanitizer or an explicit taint_unwrap().",
        "repro":
        "The accepted .hls file, the checker output, and the sink "
        "output showing the unsanitised data.",
        "out_of_scope":
        "taint_unwrap() is an explicit user decision (SECURITY.md); "
        "a user who unwraps everything has opted out, not found a bug.",
    },
    {
        "id": "G-CONC-01",
        "family": "concurrency",
        "severity": "critical",
        "claim":
        "Owned values cannot be shared across task boundaries except "
        "by clone or ownership transfer; the Send rules close the "
        "remaining channels, so data races are refused at compile "
        "time.",
        "mechanism":
        "spawn/chan.send/async_spawn/stream_send argument sharing is "
        "refused unless the value crosses as clone(x) or take(x); "
        "Task handles and Cap values are not Send and cannot cross.",
        "enforced_by": "tools/hlinterp/checking/call.py (sharing + Send rules) "
                       "+ the deterministic scheduler for testing",
        "anchors": [
            ("tools/hlinterp/checking/call.py", "cannot share"),
            ("tools/hlinterp/checking/call.py", "is not Send"),
        ],
        "probes": [
            {"path": "tests/fail/fail_spawn_shared.hls",
             "expect": "cannot share", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_send_shared.hls",
             "expect": "cannot share", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_task_not_send.hls",
             "expect": "is not Send", "kind": "refuse", "args": []},
        ],
        "violation":
        "A program accepted where two tasks can read or write the same "
        "owned value without a channel or an ownership transfer.",
        "repro":
        "The accepted .hls file, the checker output, and (if the race "
        "manifests) the divergent runs under hls-det.",
        "out_of_scope":
        "Logical deadlocks between tasks that legally share channels "
        "(the Stage 127 detector reports them; they are not memory "
        "unsafety).",
    },
    {
        "id": "G-MATCH-01",
        "family": "match",
        "severity": "high",
        "claim":
        "Every match is exhaustive over its enum's variants; a missing "
        "arm is a compile error naming the missing variant.",
        "mechanism":
        "The checker enumerates the matched enum's variants and "
        "refuses non-exhaustive matches, wrong-enum matches and "
        "wrong-arity payloads.",
        "enforced_by": "tools/hlinterp/checking/match_fx.py",
        "anchors": [
            ("tools/hlinterp/checking/match_fx.py", "not exhaustive"),
        ],
        "probes": [
            {"path": "tests/fail/fail_match_nonexhaustive.hls",
             "expect": "not exhaustive", "kind": "refuse", "args": []},
        ],
        "violation":
        "An accepted match that silently falls through for a variant "
        "it does not cover.",
        "repro":
        "The accepted .hls file and the checker output; the run "
        "reaching the uncovered variant.",
        "out_of_scope":
        "Wildcard arms (_): they are exhaustive by intent and the "
        "language documents them as such.",
    },
    {
        "id": "G-CAP-01",
        "family": "capabilities",
        "severity": "high",
        "claim":
        "Privileged builtins demand their capability as a Cap[E] "
        "value; capabilities cannot be forged, cloned or taken "
        "outside a grant.",
        "mechanism":
        "cap_take(\"E\") yields Cap[E] only inside main or a function "
        "declaring uses E; clone() refuses Cap types; unknown "
        "capability names are refused at the type level.",
        "enforced_by": "tools/hlinterp/checking/call.py (cap enforcement) + the "
                       "clone rules",
        "anchors": [
            ("tools/hlinterp/checking/call.py", "unknown capability"),
            ("tools/hlinterp/checking/call.py", "cap_take"),
        ],
        "probes": [
            {"path": "tests/fail/fail_stage111_cap_missing_grant.hls",
             "expect": "requires effect", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_stage111_cap_clone.hls",
             "expect": "is not supported", "kind": "refuse", "args": []},
            {"path": "tests/fail/fail_stage111_cap_take_outside.hls",
             "expect": "may only be called", "kind": "refuse", "args": []},
        ],
        "violation":
        "A program accepted that obtains Cap[E] without the grant, "
        "duplicates one, or passes one where the callee never "
        "declared the capability.",
        "repro":
        "The accepted .hls file and the checker output showing the "
        "ungranted capability flowing to the privileged builtin.",
        "out_of_scope":
        "The effect-system alias: Cap[E] is stricter than uses E and "
        "the two are documented as different grants (SPEC 65).",
    },
    {
        "id": "G-SBX-01",
        "family": "sandbox",
        "severity": "high",
        "claim":
        "Under --sandbox, extern blocks and Proc-reaching programs are "
        "refused outright, and the four file builtins are confined to "
        "the sandbox directory.",
        "mechanism":
        "boot.py refuses extern \"C\" blocks and any function whose "
        "computed effects reach Proc when --sandbox is active; the "
        "runtime confines read_file, read_file_tainted, write_file "
        "and file_exists to DIR, in both the interpreter and native "
        "paths.",
        "enforced_by": "tools/hlinterp/boot.py (sandbox gate) + the runtime path "
                       "confinement",
        "anchors": [
            ("tools/hlinterp/boot.py", "--sandbox is incompatible"),
            ("tools/hlinterp/boot.py", "restrict filesystem builtins"),
        ],
        "probes": [
            {"path": "tests/guarantee_probes/sandbox_extern.hls",
             "expect": "--sandbox is incompatible", "kind": "refuse",
             "args": ["--sandbox", "tests/guarantee_probes"]},
        ],
        "violation":
        "A program compiled or run under --sandbox that reads, writes "
        "or probes outside DIR, or that reaches libc through an "
        "extern block.",
        "repro":
        "The accepted command line, the .hls file, and the observed "
        "out-of-sandbox access (strace or the file appearing).",
        "out_of_scope":
        "Sandbox ESCAPE via a kernel vulnerability; the seccomp-bpf "
        "layer (Stage 110) is belt-and-braces over THIS gate, not a "
        "substitute for it.",
    },
    {
        "id": "G-CT-01",
        "family": "consttime",
        "severity": "high",
        "claim":
        "A #[ct] function is verified branch-free, loop-bound-free and "
        "secret-indexed-memory-free over its declared #[secrets] "
        "parameters; an unverifiable claim is a compile error.",
        "mechanism":
        "The checker refuses #[ct] without #[secrets], unknown secret "
        "names, duplicate claims, and any branch or memory access "
        "that depends on a secret parameter.",
        "enforced_by": "tools/hlinterp/checking/core.py (the ct verifier) + the "
                       "Stage 101 side-channel pass",
        "anchors": [
            ("tools/hlinterp/checking/core.py", "constant-time claim"),
        ],
        "probes": [
            {"path": "tests/fail/fail_stage102_ct_nosecrets.hls",
             "expect": "must name the parameters", "kind": "refuse",
             "args": []},
            {"path": "tests/fail/fail_stage101_secret_unknown_param.hls",
             "expect": "not a parameter", "kind": "refuse", "args": []},
        ],
        "violation":
        "A #[ct] claim accepted where a branch, loop bound or memory "
        "index depends on a declared secret — the classic timing or "
        "cache side channel.",
        "repro":
        "The accepted .hls file, the checker output, and (if "
        "available) the timing distribution demonstrating the leak.",
        "out_of_scope":
        "Micro-architectural leaks below the model (port contention, "
        "power) — the claim is about the verified source-level "
        "fragment, as Stage 102 documents.",
    },
    {
        "id": "G-FFI-01",
        "family": "ffi",
        "severity": "high",
        "claim":
        "Extern declarations are FFI-fenced: they demand declared "
        "effects, and taint flows through the boundary into sinks.",
        "mechanism":
        "Calling an extern requires the callee's declared effect set "
        "in the caller; #[taint_source]/#[taint_sink] attributes on "
        "externs are checked for arity and void-return honesty, and "
        "tainted args to extern sinks are refused.",
        "enforced_by": "tools/hlinterp/checking/call.py + tools/hlinterp/ffi_taint.py",
        "anchors": [
            ("tools/hlinterp/ffi_taint.py", "taint_source"),
            ("tools/hlinterp/checking/call.py", "taint-sink violation: extern"),
        ],
        "probes": [
            {"path": "tests/fail/fail_stage109_ffi_sink.hls",
             "expect": "taint-sink violation: extern", "kind": "refuse",
             "args": []},
            {"path": "tests/fail/fail_stage109_ffi_source_void.hls",
             "expect": "returns void", "kind": "refuse", "args": []},
        ],
        "violation":
        "An accepted program where an extern call escapes effect "
        "accounting, or where a tainted value enters an extern the "
        "program declared as a taint sink.",
        "repro":
        "The accepted .hls file with the extern block and attributes, "
        "plus the checker output.",
        "out_of_scope":
        "A MIS-DECLARED extern (the C side lying about its signature) "
        "— SECURITY.md scopes this honestly: the fence is sound, the "
        "C header is trusted; hlbindgen mitigates by generating from "
        "the header.",
    },
    {
        "id": "G-SPEC-01",
        "family": "types",
        "severity": "medium",
        "claim":
        "Bindings are single-assignment by default, never shadowed, "
        "always initialised, and every name resolves or the program "
        "is refused.",
        "mechanism":
        "The statement checker refuses shadowing, reassignment of "
        "immutable bindings, use of undefined names and duplicate "
        "definitions; there is no null and no uninitialised value.",
        "enforced_by": "tools/hlinterp/checking/stmt.py + the name-resolution "
                       "pass",
        "anchors": [
            ("tools/hlinterp/checking/stmt.py", "shadowing not allowed"),
        ],
        "probes": [
            {"path": "tests/fail/fail_shadow.hls",
             "expect": "shadowing not allowed", "kind": "refuse",
             "args": []},
            {"path": "tests/fail/fail_undefined_var.hls",
             "expect": "does not exist", "kind": "refuse", "args": []},
        ],
        "violation":
        "An accepted program where two bindings share a name in one "
        "scope, or a name resolves to nothing and still compiles.",
        "repro":
        "The accepted .hls file and the checker output.",
        "out_of_scope":
        "Deliberate distinct scopes (function boundaries, blocks) "
        "reusing a name — that is the language's documented scoping.",
    },
    {
        "id": "G-DET-01",
        "family": "determinism",
        "severity": "high",
        "claim":
        "The bootstrap is a fixed point: hlc compiled from its own "
        "source re-emits that source byte-identically, so every "
        "guarantee above holds identically for the Stage-0 interpreter "
        "and the native compiler.",
        "mechanism":
        "make bootstrap compiles the snapshot's native hlc and diffs "
        "its re-emission against the committed snapshot (the "
        "fixed-point law, step 3); the cross-bootstrap ladder extends "
        "the law to the freestanding binary, and `make snapshot-chain` "
        "re-proves it as two independent ignitions.",
        "enforced_by": "Makefile (bootstrap + snapshot-chain targets) + "
                       "tests/xbootstrap_acceptance.py",
        "anchors": [
            ("Makefile", "fixed-point law: the derivation must equal the snapshot"),
            ("tests/xbootstrap_acceptance.py", "byte"),
        ],
        "probes": [],
        "violation":
        "A source change after which the two bootstrap passes differ, "
        "or the freestanding compiler disagreeing with the hosted one.",
        "repro":
        "make bootstrap output showing the diff, or the "
        "xbootstrap-acceptance failure log.",
        "out_of_scope":
        "Whitespace in emitted C that carries no semantic difference "
        "— the law is the fixed point itself, which the diff enforces "
        "exactly.",
    },
    {
        "id": "G-CORPUS-01",
        "family": "determinism",
        "severity": "medium",
        "claim":
        "The interpreter and the native backend agree byte-for-byte "
        "over the entire ok corpus and the differential table, with "
        "every known divergence sanctioned in the cross-impl ledger.",
        "mechanism":
        "tests/run_tests.sh runs every tests/ok program on both "
        "implementations and diffs; hls-xdiff sits the corpus in "
        "front of three executing backends and compares bytes and "
        "halt classes against the ledger.",
        "enforced_by": "tests/suites/suite_02_differential.sh + "
                       "tools/hls-xdiff.py",
        "anchors": [
            ("tests/suites/suite_02_differential.sh",
             "native vs interpreter"),
            ("tools/hls-xdiff.py", "ledger"),
        ],
        "probes": [],
        "violation":
        "An ok-corpus program (or an xdiff case) where the two "
        "implementations disagree on bytes or halt class, unsanctioned "
        "by tests/xdiff/ledger.json.",
        "repro":
        "The program, both outputs, and the diff; note the xdiff case "
        "id if the divergence is already a ledger row.",
        "out_of_scope":
        "Sanctioned ledger rows (tests/xdiff/ledger.json): they are "
        "documented, pinned classes, not new divergences.",
    },
]
LEDGER_IDS = [g["id"] for g in LEDGER]


# ---------------------------------------------------------------------------
# Ledger validation. A ledger with a duplicate id, an unknown family,
# an unknown severity, a guarantee with no anchor, or a probe record
# that is not well-formed is a broken LEDGER — refuse before anything
# is verified. Fail-closed starts at home.
# ---------------------------------------------------------------------------
def validate_ledger():
    problems = []
    seen = set()
    for g in LEDGER:
        gid = g.get("id", "<no id>")
        if gid in seen:
            problems.append("duplicate guarantee id %s" % gid)
        seen.add(gid)
        if g.get("family") not in FAMILIES:
            problems.append("%s: unknown family %r" % (gid, g.get("family")))
        if g.get("severity") not in SEVERITIES:
            problems.append("%s: unknown severity %r"
                            % (gid, g.get("severity")))
        if not g.get("anchors"):
            problems.append("%s: no anchors — an unanchored guarantee "
                            "is a wish, not a guarantee" % gid)
        for a in g.get("anchors", []):
            if len(a) != 2:
                problems.append("%s: malformed anchor %r" % (gid, a))
        for p in g.get("probes", []):
            if p.get("kind") not in ("refuse", "panic"):
                problems.append("%s: probe kind must be refuse|panic"
                                % gid)
            if "path" not in p or "expect" not in p:
                problems.append("%s: probe needs path+expect" % gid)
        if not g.get("claim") or not g.get("violation") \
                or not g.get("repro"):
            problems.append("%s: claim/violation/repro are all "
                            "mandatory (a bounty entry maps onto "
                            "them)" % gid)
    return problems


# ---------------------------------------------------------------------------
# Anchors. A grep, honestly: the pattern must appear in the file at
# the given tree. The line number is recorded so the report can point
# at the live enforcement point.
# ---------------------------------------------------------------------------
def anchor_resolve(tree, relpath, pattern):
    """-> (ok, detail) where detail is 'line N' or the reason."""
    full = os.path.join(tree, relpath)
    if not os.path.isfile(full):
        return False, "missing anchor file: %s" % relpath
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if pattern in line:
                    return True, "line %d" % i
    except OSError as ex:
        return False, "unreadable: %s" % ex
    return False, ("pattern %r not found in %s — the enforcement point "
                   "this guarantee rests on is gone" % (pattern, relpath))


def git_sha(tree):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tree, timeout=15,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown (not a git tree)"


def ledger_digest():
    """sha256 over the canonical JSON of the ledger — the digest the
    report stamps, so a hand-edited ledger is visible in every
    document it produced."""
    blob = json.dumps(LEDGER, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Probes. The real toolchain judges: boot.py --check for refusals,
# boot.py for runtime panics. Nothing here trusts the ledger's own
# opinion about the code — the checker is the only witness.
# ---------------------------------------------------------------------------
def probe_run(tree, probe):
    """-> (verdict, detail). verdict in ok|violated|error."""
    path = os.path.join(tree, probe["path"])
    if not os.path.isfile(path):
        return "error", "probe file missing: %s" % probe["path"]
    args = [sys.executable, BOOT]
    args += probe.get("args", [])
    if probe["kind"] == "refuse":
        args += ["--check", path]
    else:
        args += [path]
    try:
        out = subprocess.run(args, cwd=tree, timeout=120,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "violated", ("probe timed out (expected %s)"
                            % probe["kind"])
    blob = (out.stdout or "") + (out.stderr or "")
    if probe["kind"] == "refuse":
        if out.returncode == 1 and probe["expect"] in blob:
            return "ok", "refused: %r" % _first_line(blob)
        if out.returncode == 0:
            return "violated", ("probe was ACCEPTED — expected refusal "
                                "naming %r" % probe["expect"])
        return "violated", ("refused with the wrong message "
                            "(exit %d): %r" % (out.returncode,
                                               _first_line(blob)))
    # panic probe: exit 101, message pinned
    if out.returncode == 101 and probe["expect"] in blob:
        return "ok", "panicked: %r" % _first_line(blob)
    if out.returncode == 0:
        return "violated", ("probe ran clean — expected panic %r"
                            % probe["expect"])
    return "violated", ("wrong halt (exit %d): %r"
                        % (out.returncode, _first_line(blob)))


def _first_line(blob):
    for ln in (blob or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln[:120]
    return "<no output>"


# ---------------------------------------------------------------------------
# verify — the anti-rot pass. Anchors, probes, policy anchors; every
# miss is a named refusal. Returns (verdict, results-dict).
# ---------------------------------------------------------------------------
def verify(tree, quiet=False):
    results = {
        "policy": POLICY,
        "tree": git_sha(tree),
        "ledger_sha256": ledger_digest(),
        "guarantees": [],
        "refusals": [],
    }
    problems = validate_ledger()
    for p in problems:
        results["refusals"].append("ledger: %s" % p)

    for a_rel, a_pat in POLICY_ANCHORS:
        okc, detail = anchor_resolve(tree, a_rel, a_pat)
        if not okc:
            results["refusals"].append(
                "policy: the reporting channel anchor is gone "
                "(%s: %s)" % (a_rel, detail))

    for g in LEDGER:
        gid = g["id"]
        rec = {"id": gid, "family": g["family"],
               "severity": g["severity"], "anchors": [],
               "probes": [], "status": "verified"}
        for rel, pat in g["anchors"]:
            okc, detail = anchor_resolve(tree, rel, pat)
            rec["anchors"].append({"file": rel, "pattern": pat,
                                   "ok": okc, "detail": detail})
            if not okc:
                rec["status"] = "void"
                results["refusals"].append("%s: anchor void — %s"
                                           % (gid, detail))
        for probe in g["probes"]:
            verdict, detail = probe_run(tree, probe)
            rec["probes"].append({"path": probe["path"],
                                  "kind": probe["kind"],
                                  "verdict": verdict,
                                  "detail": detail})
            if verdict != "ok":
                rec["status"] = "violated" if verdict == "violated" \
                    else "error"
                results["refusals"].append("%s: probe %s — %s"
                                           % (gid, probe["path"],
                                              detail))
        if rec["status"] == "verified" and not quiet:
            n_a = sum(1 for a in rec["anchors"] if a["ok"])
            n_p = sum(1 for p in rec["probes"]
                      if p["verdict"] == "ok")
            print("  verified  %-14s %-12s %-8s anchors %d probes %d"
                  % (gid, g["family"], g["severity"], n_a, n_p))
        results["guarantees"].append(rec)

    results["verdict"] = "verified" if not results["refusals"] \
        else "violated"
    return results["verdict"] == "verified", results


# ---------------------------------------------------------------------------
# The report. The bug-bounty scope document, regenerated from the
# ledger — never hand-written, never written from a violated ledger.
# Deterministic: fixed order, no wall-clock unless --stamp.
# ---------------------------------------------------------------------------
def render_report(tree, results, stamp=None):
    L = []
    sha = results["tree"]
    n_ok = sum(1 for r in results["guarantees"]
               if r["status"] == "verified")
    n_a = sum(1 for r in results["guarantees"]
              for a in r["anchors"] if a["ok"])
    n_p = sum(1 for r in results["guarantees"]
              for p in r["probes"] if p["verdict"] == "ok")
    L.append("# Halis soundness guarantees — bug-bounty scope")
    L.append("")
    L.append("Regenerated from the Stage 135 guarantee ledger"
             " (`tools/hls-guarantee.py`).")
    L.append("This document is generated, not maintained: what it")
    L.append("claims is exactly what the tree below still")
    L.append("demonstrates. Do not hand-edit; regenerate with")
    L.append("`python3 tools/hls-guarantee.py report --out"
             " SOUNDNESS.md`.")
    L.append("")
    L.append("- Policy: %s" % POLICY)
    L.append("- Tree: `%s`" % sha)
    L.append("- Ledger digest: `sha256:%s`"
             % results["ledger_sha256"])
    L.append("- Verification: **%s** — %d/%d guarantees"
             " re-demonstrated (%d anchors resolved, %d probes green)"
             % (results["verdict"], n_ok, len(LEDGER), n_a, n_p))
    if stamp:
        L.append("- Generated: %s" % stamp)
    L.append("")
    L.append("## What the ledger is")
    L.append("")
    L.append("Every row is a soundness guarantee the Halis toolchain")
    L.append("makes, with three live parts: the ANCHORS (enforcement")
    L.append("points grepped in this tree — a deleted enforcement")
    L.append("point voids the guarantee before any bounty argument")
    L.append("starts), the PROBES (real programs re-run through the")
    L.append("real checker and interpreter at report time — refusal")
    L.append("messages pinned), and the CONTRACT (what a")
    L.append("bounty-eligible violation of this row looks like).")
    L.append("A report that cannot be regenerated is a report nobody")
    L.append("can audit; the digest above pins the ledger this")
    L.append("document was rendered from.")
    L.append("")
    L.append("## Eligibility — what a bounty pays for")
    L.append("")
    L.append("A report is bounty-eligible when it names one ledger")
    L.append("entry, ships a reproducer the toolchain ACCEPTS (or a")
    L.append("command line for the runtime rows), and demonstrates")
    L.append("the violation that entry's contract describes — at the")
    L.append("boundary the guarantee claims, not at a boundary nobody")
    L.append("claimed. Reports outside every contract land in the")
    L.append("out-of-scope register below and are closed, not paid.")
    L.append("")
    L.append("## Severity vocabulary")
    L.append("")
    L.append("| severity | rank | meaning |")
    L.append("|----------|-----:|---------|")
    for sev in SEVERITIES:
        L.append("| %s | %d | %s |"
                 % (sev, SEVERITY_RANK[sev], SEVERITY_MEANING[sev]))
    L.append("")
    L.append("## The guarantees")
    L.append("")
    for g, rec in zip(LEDGER, results["guarantees"]):
        L.append("### %s — %s — %s"
                 % (g["id"], g["family"], g["severity"]))
        L.append("")
        L.append("**Claim.** %s" % g["claim"])
        L.append("")
        L.append("**Mechanism.** %s" % g["mechanism"])
        L.append("")
        L.append("**Enforced by.** %s" % g["enforced_by"])
        L.append("")
        L.append("**Anchors (verified at this tree).**")
        L.append("")
        for a in rec["anchors"]:
            mark = "yes" if a["ok"] else "VOID"
            L.append("- `%s` — \"%s\" (%s: %s)"
                     % (a["file"], a["pattern"], mark, a["detail"]))
        if rec["probes"]:
            L.append("")
            L.append("**Demonstrated by (re-run at this tree).**")
            L.append("")
            for p in rec["probes"]:
                mark = {"ok": "green"}.get(p["verdict"],
                                           p["verdict"].upper())
                L.append("- `%s` (%s) — %s: %s"
                         % (p["path"], p["kind"], mark,
                            p["detail"]))
        L.append("")
        L.append("**Violation contract.** %s" % g["violation"])
        L.append("")
        L.append("**Reproduction.** %s" % g["repro"])
        L.append("")
        L.append("**Out of scope.** %s" % g["out_of_scope"])
        L.append("")
    L.append("## Out of scope — the honest boundary")
    L.append("")
    L.append("The register below is the aggregate of what the")
    L.append("toolchain does NOT guarantee, carried over from")
    L.append("SECURITY.md's honesty section and kept current by the")
    L.append("ledger:")
    L.append("")
    L.append("- a user who calls `taint_unwrap()` has opted out of")
    L.append("  the taint system — that is the documented escape,")
    L.append("  not a bug;")
    L.append("- a mis-declared `extern \"C\"` (the C side lying about")
    L.append("  its signature) is trusted by design; `hlbindgen`")
    L.append("  mitigates by generating from the header;")
    L.append("- deep native recursion can exhaust the OS stack —")
    L.append("  the interpreter panics cleanly, the native binary")
    L.append("  uses the OS stack (documented in SECURITY.md);")
    L.append("- the proof engine reasons about its documented")
    L.append("  fragment only; outside it, `-O fast` keeps the")
    L.append("  runtime check conservatively;")
    L.append("- micro-architectural side channels below the #[ct]")
    L.append("  source-level model (port contention, power, EM);")
    L.append("- kernel/OS vulnerabilities under the sandbox — the")
    L.append("  sandbox gates the toolchain's own surface;")
    L.append("- sanctioned `tests/xdiff/ledger.json` rows — pinned,")
    L.append("  documented divergence classes, not new ones.")
    L.append("")
    L.append("## Reporting")
    L.append("")
    L.append("The channel is quoted from SECURITY.md live (the ledger")
    L.append("anchors it): open a private GitHub Security Advisory at")
    L.append("<https://github.com/mhieuhonda/halis-lang/security/advisories/new>")
    L.append("— never a public issue. The project follows a 90-day")
    L.append("coordinated-disclosure window and aims to acknowledge")
    L.append("within 48 hours.")
    L.append("")
    L.append("## The reproduction contract")
    L.append("")
    L.append("Every eligible report carries:")
    L.append("")
    L.append("1. the ledger id (e.g. `G-TAINT-01`) the claim violates;")
    L.append("2. a minimal `.hls` reproducer (one file, or one command")
    L.append("   line for the runtime rows) the toolchain ACCEPTS;")
    L.append("3. the toolchain used — commit sha, boot.py vs `bin/hlc`")
    L.append("   or both (a divergence report names both outputs);")
    L.append("4. observed vs expected, in the terms of the row's")
    L.append("   violation contract;")
    L.append("5. for runtime rows: the exit code and the panic line.")
    L.append("")
    L.append("Reports missing the id are triaged against the ledger;")
    L.append("reports the ledger cannot name are out of scope by")
    L.append("definition — extend the ledger first, then the bounty")
    L.append("applies to the new row.")
    L.append("")
    return "\n".join(L) + "\n"
# ---------------------------------------------------------------------------
# The CLI.
# ---------------------------------------------------------------------------
def cmd_list(args):
    if args.json:
        rows = [{"id": g["id"], "family": g["family"],
                 "severity": g["severity"], "claim": g["claim"],
                 "anchors": len(g["anchors"]),
                 "probes": len(g["probes"])}
                for g in LEDGER
                if args.family is None
                or g["family"] == args.family]
        print(json.dumps({"policy": POLICY,
                          "ledger_sha256": ledger_digest(),
                          "guarantees": rows}, indent=2,
                         sort_keys=True))
        return 0
    rows = [g for g in LEDGER
            if args.family is None or g["family"] == args.family]
    if args.family is not None and not rows:
        sys.stderr.write("error: unknown family %r (families: %s)\n"
                         % (args.family, ", ".join(FAMILIES)))
        return 2
    print("hls-guarantee %s — %d guarantees (policy %s)"
          % ("|".join(FAMILIES) if args.family is None
             else args.family, len(rows), POLICY))
    for g in rows:
        print("  %-14s %-12s %-8s %s"
              % (g["id"], g["family"], g["severity"], g["claim"]))
    return 0


def cmd_show(args):
    gid = args.id
    for g in LEDGER:
        if g["id"] == gid:
            print("%s — %s — %s" % (g["id"], g["family"],
                                    g["severity"]))
            print()
            print("Claim      : %s" % g["claim"])
            print("Mechanism  : %s" % g["mechanism"])
            print("Enforced by: %s" % g["enforced_by"])
            print("Anchors    :")
            for rel, pat in g["anchors"]:
                print("  - %s : %r" % (rel, pat))
            if g["probes"]:
                print("Probes     :")
                for p in g["probes"]:
                    print("  - %s (%s) expects %r"
                          % (p["path"], p["kind"], p["expect"]))
            print("Violation  : %s" % g["violation"])
            print("Repro      : %s" % g["repro"])
            print("Out of scope: %s" % g["out_of_scope"])
            return 0
    sys.stderr.write("error: unknown guarantee id %r "
                     "(known: %s)\n" % (gid, ", ".join(LEDGER_IDS)))
    return 2


def cmd_verify(args):
    tree = os.path.abspath(args.root) if args.root else ROOT
    if not os.path.isdir(tree):
        sys.stderr.write("error: --root is not a directory: %s\n"
                         % tree)
        return 2
    # --json means machine-readable ONLY: the progress lines stay off
    # stdout (they would corrupt the stream).
    okc, results = verify(tree, quiet=args.quiet or args.json)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True,
                         ensure_ascii=True))
    if not okc:
        if not args.json:
            sys.stderr.write("LEDGER VIOLATED — %d refusal(s):\n"
                             % len(results["refusals"]))
            for r in results["refusals"]:
                sys.stderr.write("  - %s\n" % r)
        else:
            sys.stderr.write("LEDGER VIOLATED — see the refusals "
                             "array\n")
        return 1
    if not args.json and not args.quiet:
        print("LEDGER VERIFIED: %d guarantees, %d anchors, "
              "policy anchors green"
              % (len(results["guarantees"]),
                 sum(len(r["anchors"])
                     for r in results["guarantees"])))
    return 0


def cmd_report(args):
    if not args.out:
        sys.stderr.write("error: report needs --out FILE.md\n")
        return 2
    tree = os.path.abspath(args.root) if args.root else ROOT
    if not os.path.isdir(tree):
        sys.stderr.write("error: --root is not a directory: %s\n"
                         % tree)
        return 2
    okc, results = verify(tree, quiet=True)
    if not okc:
        sys.stderr.write("error: the ledger is violated — no report "
                         "is written from a violated ledger. Refusals:\n")
        for r in results["refusals"]:
            sys.stderr.write("  - %s\n" % r)
        return 1
    stamp = None
    if args.stamp:
        stamp = subprocess.run(
            ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], timeout=10,
            stdout=subprocess.PIPE, text=True).stdout.strip()
    doc = render_report(tree, results, stamp=stamp)
    out = os.path.abspath(args.out)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    if args.json:
        with open(os.path.abspath(args.json), "w",
                  encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, sort_keys=True,
                      ensure_ascii=True)
            fh.write("\n")
    print("report: %s (%d guarantees, tree %s)"
          % (os.path.relpath(out, ROOT) if out.startswith(ROOT)
             else out, len(results["guarantees"]),
             results["tree"][:12]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-guarantee",
        description="the Stage 135 soundness guarantee ledger")
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="the ledger table")
    p_list.add_argument("--family", default=None)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(fn=cmd_list)

    p_show = sub.add_parser("show", help="one guarantee in full")
    p_show.add_argument("id")
    p_show.set_defaults(fn=cmd_show)

    p_ver = sub.add_parser("verify", help="re-demonstrate every "
                                          "guarantee at this tree")
    p_ver.add_argument("--root", default=None)
    p_ver.add_argument("--json", action="store_true")
    p_ver.add_argument("--quiet", action="store_true")
    p_ver.set_defaults(fn=cmd_verify)

    p_rep = sub.add_parser("report", help="regenerate the scope "
                                          "document")
    p_rep.add_argument("--out", required=True)
    p_rep.add_argument("--json", default=None)
    p_rep.add_argument("--stamp", action="store_true")
    p_rep.add_argument("--root", default=None)
    p_rep.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help(sys.stderr)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
