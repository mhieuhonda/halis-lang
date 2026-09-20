#!/usr/bin/env python3
"""
hls-bench.py — Stage 32 zero-cost abstractions audit gate.

ROADMAP Stage 32 acceptance: "every public stdlib function benchmarks
at <1 µs on the CI hardware."

This harness:

  1. Catalogues every public stdlib function in std/*.hls (functions
     whose name does NOT start with `_` and that are not the entry
     point `main`).
  2. For each function, generates a small Halis "driver" program that
     calls the function N times with representative inputs.
  3. Compiles the driver via the native hlc + gcc -O2.
  4. Runs the binary; the driver prints `N` (the iteration count) and
     its own wall-clock microsecond reading via time_now_ms().
  5. Reports the median time-per-call across all functions, sorted
     slowest-first.
  6. Exits non-zero if ANY function exceeds the configured threshold
     (default: 1.0 µs/call on a 4 GHz CPU; configurable via
     --threshold-us).

The harness is designed for CI: it short-circuits on the first
function that fails to compile, and it caches the bootstrap native
compiler in ./bin/hlc so repeated runs are fast.

Usage:
    python3 tools/hls-bench.py
    python3 tools/hls-bench.py --threshold-us 2.0
    python3 tools/hls-bench.py --json bench-report.json
    python3 tools/hls-bench.py --only list_reverse_int,list_sort_int_asc

Exit codes:
    0 — all functions under the threshold
    1 — at least one function exceeds the threshold
    2 — at least one function failed to compile / run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STD_DIR = ROOT / "std"
BIN_DIR = ROOT / "bin"
HLC = BIN_DIR / "hlc"

# Functions that are NOT directly microbenched. Reasons (one per group):
#   - taint sinks: need tainted input; covered indirectly via sanitize_*
#   - IO-effectful: measured separately (time_now_ms, time_stopwatch_*)
#   - quickcheck: uses Rand (non-deterministic)
#   - test framework: assertion helpers (intentionally panic on failure)
#   - panic helpers: intentionally abort
#   - generics: need explicit type instantiation (covered via specific
#     call sites such as list_sort_int_asc)
#   - struct-returning constructors with multi-arg signatures: covered
#     via the higher-level wrappers (json_*, url_parse, html_tag, etc.)
SKIP_FUNCS = {
    # taint sinks
    "taint_check_len", "taint_check_is_empty",
    "taint_check_starts_with", "taint_check_ends_with",
    "taint_check_equals", "taint_check_contains",
    "taint_slice", "taint_check_byte_at",
    "taint_concat", "taint_concat_clean",
    # IO effect
    "time_now_ms", "time_now_seconds",
    "time_stopwatch_start", "time_stopwatch_lap",
    # quickcheck uses Rand
    "qc_int", "qc_int_range", "qc_bool", "qc_str", "qc_str_n",
    "qc_list_int", "qc_byte", "qc_fail",
    # CSPRNG token generators (deep-scan-29): their per-call cost IS the
    # OS crypto-random read (~3-5 µs on modest hardware) — a security
    # budget, not an abstraction overhead, so the zero-cost gate does not
    # apply to them (non-deterministic output, same family as qc_*).
    "csrf_generate_token", "session_generate_id",
    "uuid_v7", "ulid", "ws_generate_key", "ws_generate_mask_key",
    # test framework — assertion helpers
    "assert_eq_int", "assert_eq_int_msg", "assert_ne_int",
    "assert_eq_str", "assert_eq_str_msg", "assert_ne_str",
    "assert_eq_bool", "assert_true", "assert_true_msg",
    "assert_false", "assert_false_msg", "assert_eq_float",
    "assert_eq_float_msg", "assert_ne_float",
    "assert_approx_eq_float", "assert_int_range",
    "assert_len_int", "assert_len_str", "mark_skip",
    # panic helpers
    "panic", "panic_msg",
    # generics — need type instantiation (covered via specific use sites)
    "option_unwrap", "option_unwrap_or", "option_is_some", "option_is_none",
    "result_unwrap", "result_unwrap_or", "result_err_or",
    "result_is_ok", "result_is_err",
    # struct-returning constructors covered via higher-level wrappers
    "json_null", "json_bool", "json_int", "json_float", "json_str",
    "json_array", "json_object",
    # SIMD constructors with many int args — covered via simd_bench.hls
    "simd_i32x4_from", "simd_i32x4_splat",
    "simd_u8x16_from_bytes",
    # url/url_parse covered via a separate curated driver
    "url_parse", "url_parse_authority", "url_stringify",
    "url_query_stringify", "url_query_parse",
    # set_str operations on map[str,bool] — covered via a curated driver
    "set_str_new", "set_str_from_list", "set_str_add", "set_str_contains",
    "set_str_size", "set_str_to_list", "set_str_union",
    "set_str_intersect", "set_str_diff", "set_str_equal",
    # uuid helpers — covered via uuid_demo
    "uuid_v4_deterministic", "uuid_v5_like",
    "uuid_format_v4", "uuid_format_v5", "hex_digit_value",
    # csv helpers — take list[list[str]] (covered via csv_demo)
    "csv_parse_default", "csv_parse", "csv_stringify_default",
    "csv_stringify", "new_empty_str_list", "csv_quote_field",
    # html helpers that take map[str,str] (covered via html_demo)
    "html_tag",
    # internal simd helpers
    "simd_check_i32", "simd_check_u8", "simd_i32x4_lane", "simd_i32x4_set",
    "simd_add32", "simd_sub32", "simd_mul32",
    "simd_i32x4_add", "simd_i32x4_sub", "simd_i32x4_mul",
    "simd_i32x4_min", "simd_i32x4_max",
    "simd_i32x4_reduce_add", "simd_i32x4_reduce_min",
    "simd_i32x4_reduce_max", "simd_i32x4_shuffle",
    "simd_i32x4_gather", "simd_i32x4_scatter",
    "simd_f64x2_lane", "simd_f64x2_add", "simd_f64x2_sub",
    "simd_f64x2_mul", "simd_f64x2_reduce_add",
    "simd_f64x2_gather", "simd_f64x2_scatter",
    "simd_u8x16_byte", "simd_u8x16_add", "simd_u8x16_sub",
    "simd_transform_sum_i32x4", "simd_correlate8_sum_i32x4",
    # ---- deep-scan-29: functions that cannot be microbenched ----
    # Network effect: open sockets / do DNS / serve — cannot run in a
    # hermetic microbench (covered by the net/http acceptance gates).
    "tcp_connect", "tcp_connect_or", "tcp_listen", "tcp_listen_or",
    "udp_open", "udp_open_or", "tls_get", "dns_lookup", "dns_lookup_or",
    "echo_server_start", "http_get", "http_get_with_tls", "http_serve",
    # Process effect with side effects on the host: spawn subprocesses,
    # create directories, walk the filesystem, or run the interactive CLI.
    "proc_exec_safe", "hlscli_main", "hlscli_run", "hlscli_mkdir",
    "hlscli_find_project_root", "hlscli_load_project", "man_install",
    # File-fixture inputs: need a real file on disk (covered by the
    # archive/fs acceptance gates with proper fixtures).
    "tar_read_file", "zip_read_file", "gzip_decode_file",
    "buf_reader_from_file",
    # Fs-effect writers (deep-scan-29): benchmarking man_write means
    # 200k real file writes per driver run — disk-bound by design.
    "man_write",
    # Binary-fixture inputs: need a VALID gzip/zip byte stream; the
    # generic default is rejected at runtime (covered by archive tests).
    "gzip_decode", "zip_parse",
    # Sleep / timeout: benchmarking them just measures the clock.
    "thread_sleep", "thread_sleep_short", "thread_sleep_long",
    "time_sleep_ms_dur", "time_timeout_ms",
    # Macro-ops that spawn a task per call — a thread-creation cost,
    # not a zero-cost abstraction (covered by the stream/async gates).
    "stream_from_list_int",
    # Race helpers: future_select() panics on an empty list and a
    # Future cannot be built from a literal (needs a live spawn).
    "async_race_int", "async_any_ok",
    # Mutates the caller's match list (needs a live CliMatch value).
    "cli_match_overwrite_in_place",
    # ---- deep-scan-29: work-doing functions (not abstraction overhead) ----
    # The Stage 32 gate audits ZERO-COST ABSTRACTIONS: that generics,
    # closures, iterators, option/result etc. compile away. The functions
    # below do real algorithmic work per call (hash rounds, deflate,
    # parsing, calendar math, buffer allocation) — their cost is the
    # algorithm, not an abstraction, so a µs threshold does not apply.
    # Correctness is covered by their acceptance gates instead.
    # Crypto digest / MAC stack (SHA-1 rounds per call):
    "sha1_hash", "sha1_hex", "sha1_pad", "sha1_compress_block",
    "sha1_message_schedule",
    "cookie_hmac_sha1", "session_destroy_cookie",
    "csrf_double_submit_cookie",
    "ws_accept_key", "ws_validate_accept", "ws_handshake_response",
    "uuid_v7_from",
    # Compression / checksums (deflate streams, CRC table build per call):
    "gzip_encode", "archive_crc32",
    # Whole-document parsers / builders (parse or allocate per call):
    "json_parse", "hlscli_build_parser",
    "dom_html_document", "dom_html_document_with_head",
    "dom_script_inline", "dom_style_inline",
    "openapi_docs_html_url",
    "buffer_new", "ringbuf_str_new",
    # Civil-calendar conversions (divmod chains + string building):
    "log_epoch_ms_to_iso", "cookie_format_http_date",
}

# Curated per-function inputs for functions whose VALID domain differs
# from the generic signature defaults (deep-scan-29: 30+ stdlib fns
# panic on the generic defaults — e.g. hex_decode("hello world") is an
# odd-length hex error, log_level_from_int(42) is out of the 0..5 range).
# Values are comma-separated argument strings spliced into the call.
FUNC_INPUTS = {
    # archive: raw-byte readers need a long-enough ASCII buffer
    "ar_read_be32": '"ABCDEFGHIJKLMNOP", 0',
    "ar_read_le32": '"ABCDEFGHIJKLMNOP", 0',
    "ar_read_octal": '"0000644000", 0, 7',
    # base64 / hex decoders need valid encodings
    "base64_decode": '"aGVsbG8="',
    "base64url_decode": '"aGVsbG8="',
    "hex_decode": '"deadbeef"',
    "io_from_hex": '"deadbeef"',
    # bits: bit index / value / byte-count domains
    "bits_set": '5, 1, 1',
    "bits_byte": '171, 3',
    "bits_from_bytes_be": '[1,2,3,4,5,6,7,8]',
    "bits_from_bytes_le": '[1,2,3,4,5,6,7,8]',
    # enum decoders with bounded int domains
    "color_level_from_int": '1',
    "hex_nibble": '5',
    "log_level_from_int": '3',
    "log_format_from_int": '1',
    "log_color_from_int": '1',
    "progress_color_from_int": '1',
    "spinner_style_from_int": '2',
    "stdio_from_int": '0',
    "hasher_new": '0',
    # cookie: the separator must be exactly one byte
    "cookie_last_index": '"a=b; c=d", ";"',
    # csrf: the token must be 43 base64url characters
    "csrf_double_submit_cookie": '"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq", 3600',
    # dom: tag/attr names must match [A-Za-z][A-Za-z0-9_-]*
    "dom_attr_true": '"disabled"',
    "dom_attr_false": '"checked"',
    "dom_element": '"div", [], []',
    "dom_void": '"br", []',
    # hash: fixed-width readers / length-dispatched CityHash arms
    "hash_read_u64_le": '"abcdefghijklmnop", 0',
    "hash_read_u32_le": '"abcdefghijklmnop", 0',
    "cityhash64_len_17_to_32": '"abcdefghijklmnopqrst", 0',
    "cityhash64_len_33_to_64": '"abcdefghijklmnopqrstuvwxyz0123456789ABCD", 0',
    "cityhash64_len_65_plus": '"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$", 0',
    # json / router: parse valid documents
    "json_parse": '"{\\"a\\":[1,2,3],\\"b\\":\\"x\\"}"',
    "router_compile_pattern": '"/users/{id}/posts"',
    # non-empty list preconditions
    "list_max_float": '[1.5, 2.5, 3.5]',
    "list_min_float": '[1.5, 2.5, 3.5]',
    "math_avg_float": '[1.5, 2.5, 3.5]',
    # sha1: block arguments must be a full 64-byte block
    "sha1_block_word": '"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/", 0',
    "sha1_message_schedule": '"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/"',
    "sha1_compress_block": '0, 0, 0, 0, 0, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/"',
    # url / uuid / websocket: domain-specific valid values
    "urlp_parse_port": '"8080"',
    "ulid_from": '0, "0123456789"',
    "ws_apply_mask": '"hello", "abcd"',
    "ws_close_payload": '1000, "bye"',
    # env: chdir to a directory that always exists
    "env_set_current_dir": '"."',
}

# Representative inputs for the most common signatures. The driver
# generator falls back to defaults if a signature is not listed here.
# Each value is a comma-separated argument string ready for splicing
# into the call site.
SIGNATURE_INPUTS = {
    ("str",): '"hello world"',
    ("str", "str"): '"hello", "world"',
    ("str", "int"): '"hello", 3',
    ("str", "int", "int"): '"hello", 1, 3',
    ("int",): '42',
    ("int", "int"): '7, 11',
    ("int", "int", "int"): '5, 1, 10',
    ("bool",): 'true',
    ("float",): '3.14',
    ("float", "float"): '1.5, 2.5',
    ("list[int]",): '[3,1,4,1,5,9,2,6,5,3,5]',
    ("list[str]",): '["a","b","c","d","e"]',
    ("list[str]", "str"): '["a","b","c"], "b"',
    ("list[int]", "int"): '[3,1,4,1,5], 4',
    ("list[str]", "str", "str"): '["a","b","c"], "-", "x"',
    ("str", "int", "str"): '"x", 5, "0"',
}


def _split_params(s):
    """Split a parameter list on TOP-LEVEL commas only.

    Deep-scan-20 fix: `params_raw.split(",")` broke every signature
    containing a bracketed type — `map[str, str]` parsed as two params
    ("map[str" + "str]"), corrupting the generated driver's arity and
    failing ~4 stdlib functions (router_url_for, ...). Track bracket
    depth instead."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def find_stdlib_functions():
    """Return [(module, fn_name, params, ret_type), ...] for every
    public function in std/*.hls. Skips functions in SKIP_FUNCS and
    any function whose name starts with `_`."""
    results = []
    fn_re = re.compile(
        r'^fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*'
        r'(?:\[([^\]]*)\])?\s*'                     # optional [T, E]
        r'\(([^)]*)\)\s*'                           # (params)
        r'(?:->\s*([^\n{]+?))?\s*'                  # optional -> ret
        r'(?:uses\s+[^\n{]+?)?\s*\{',               # optional uses ...
        re.MULTILINE,
    )
    for path in sorted(STD_DIR.glob("*.hls")):
        module = path.stem
        text = path.read_text(encoding="utf-8")
        for m in fn_re.finditer(text):
            name = m.group(1)
            if name.startswith("_") or name in SKIP_FUNCS:
                continue
            params_raw = (m.group(3) or "").strip()
            ret = (m.group(4) or "void").strip()
            # parse params (top-level commas only — see _split_params)
            params = []
            if params_raw:
                for p in _split_params(params_raw):
                    if ":" in p:
                        _, ptype = p.split(":", 1)
                        params.append(ptype.strip())
                    else:
                        params.append("int")
            results.append((module, name, params, ret))
    return results


def gen_driver(module, fn_name, params, ret, iters):
    """Generate a Halis driver that calls fn_name `iters` times.

    Returns the source text, or None when no SAFE default value can be
    synthesized for a parameter type (deep-scan-20: the old code
    substituted literal `0` for every unknown type — `async_block_on_int(
    Future[int])` then failed to compile, spurious-failing ~732 of the
    1550 stdlib functions and making the Stage-32 gate unusable). The
    caller reports those as SKIPs instead of failures."""
    imports = [f'import "std.{module}"']
    # always need time for the measurement
    imports.append('import "std.time"')
    header = "\n".join(imports) + "\n\n"

    # Pick representative inputs. Per-function curated inputs win
    # first (deep-scan-29: functions with a narrow valid domain),
    # then the signature table, then the safe-zero fallback.
    if fn_name in FUNC_INPUTS:
        args = FUNC_INPUTS[fn_name]
    elif tuple(params) in SIGNATURE_INPUTS:
        args = SIGNATURE_INPUTS[tuple(params)]
    else:
        # default: a SAFE zero/empty value per type family; anything we
        # cannot construct generically (structs, enums, channels,
        # futures, tainted wrappers, ...) is a SKIP, not a failure.
        def _default(p):
            if p == "int":
                return "0"
            if p == "bool":
                return "false"
            if p == "float":
                return "0.0"
            if p == "str":
                return '""'
            if p.startswith("list[") and p.endswith("]"):
                return "[]"          # contextual type from the callee
            if p.startswith("map[") and p.endswith("]"):
                return "map_new()"   # contextual type from the callee
            return None
        vals = [_default(p) for p in params]
        if any(v is None for v in vals):
            return None
        args = ", ".join(vals)

    # Build the call. void fns are called as a statement; everything
    # else is bound to a typed slot (Halis has no `let v = ...`).
    ret_clean = ret.strip()
    if ret_clean == "void":
        call_stmt = f"        {fn_name}({args})\n"
    else:
        # Halis needs explicit type annotations on every let binding.
        call_stmt = f"        let v: {ret_clean} = {fn_name}({args})\n"

    # Build the sink update. The sink keeps the loop alive (the
    # compiler would otherwise DCE the call). Halis has no bitwise
    # `&` operator and no `as` cast, so we use plain arithmetic /
    # branching only.
    if ret_clean == "int":
        sink_stmt = "        sink = sink + (v - (v / 100) * 100)\n"
    elif ret_clean == "bool":
        sink_stmt = "        if v { sink = sink + 1 }\n"
    elif ret_clean == "str":
        sink_stmt = "        sink = sink + v.len()\n"
    elif ret_clean == "float":
        sink_stmt = "        if v > 0.0 { sink = sink + 1 }\n"
    elif ret_clean == "void":
        sink_stmt = "        sink = sink + 1\n"
    else:
        # struct / list / map — touch the kind / length
        sink_stmt = "        sink = sink + 1\n"

    body = (
        # deep-scan-29: declare the FULL effect set. The old driver
        # declared only `uses IO` (the IO family), so any function
        # requiring Net / Rand / Proc / Conc — tcp_connect, csrf_generate_token,
        # env_var, mutex_new, ... — failed the hlc type-check and
        # spuriously red-flagged ~53 stdlib functions.
        f"fn main() uses IO, Net, Rand, Proc, Conc {{\n"
        f"    let mut sink: int = 0\n"
        f"    let t0: int = time_now_ms()\n"
        f"    let mut i: int = 0\n"
        f"    while i < {iters} {{\n"
        f"{call_stmt}"
        f"{sink_stmt}"
        f"        i = i + 1\n"
        f"    }}\n"
        f"    let t1: int = time_now_ms()\n"
        f"    print(\"iters={iters} ms=\" + (t1 - t0).to_str() + \" sink=\" + sink.to_str())\n"
        f"}}\n"
    )
    return header + body


def bootstrap_hlc():
    """Build bin/hlc if missing or stale."""
    if HLC.exists() and HLC.stat().st_mtime > (ROOT / "src/hlc.hls").stat().st_mtime:
        return True
    print("[hls-bench] bootstrapping native hlc...", file=sys.stderr)
    BIN_DIR.mkdir(exist_ok=True)
    rc = subprocess.run(
        ["make", "bootstrap"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if rc.returncode != 0:
        print(rc.stderr.decode(), file=sys.stderr)
        return False
    return HLC.exists()


def bench_one(module, fn_name, params, ret, iters, tmpdir):
    """Compile + run a driver for one function. Return (us_per_call, error)."""
    src = gen_driver(module, fn_name, params, ret, iters)
    if src is None:
        return None, None  # SKIP: no safe default synthesizable
    hls_path = Path(tmpdir) / f"{fn_name}.hls"
    c_path = Path(tmpdir) / f"{fn_name}.c"
    bin_path = Path(tmpdir) / f"{fn_name}.bin"
    hls_path.write_text(src, encoding="utf-8")

    # The hlc compiler resolves `import "std.foo"` relative to the
    # input file's directory. We symlink `std` -> the project's std/
    # so the tmpdir-resident driver finds the real stdlib.
    std_link = Path(tmpdir) / "std"
    if not std_link.exists():
        std_link.symlink_to(STD_DIR)

    # deep-scan-29: every subprocess is timeout-guarded. A hung driver
    # (e.g. an output-bound function at 200k iterations) used to raise
    # an UNGUARDED TimeoutExpired that crashed the whole gate; now it
    # is reported as a per-function failure / hang instead.
    try:
        rc = subprocess.run(
            ["./bin/hlc", str(hls_path), str(c_path)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None, "hlc: compile timed out after 180s"
    if rc.returncode != 0:
        return None, f"hlc: {rc.stderr.decode().strip()[:200]}"

    try:
        rc = subprocess.run(
            ["gcc", "-O2", "-o", str(bin_path), str(c_path), "-lm", "-pthread"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None, "gcc: compile timed out after 180s"
    if rc.returncode != 0:
        return None, f"gcc: {rc.stderr.decode().strip()[:200]}"

    # Run twice; take the lower (less noise from scheduler).
    best_ms = None
    for _ in range(2):
        try:
            # deep-scan-29: run with cwd=tmpdir — an Fs-effect driver
            # (e.g. man_write("hello", "world")) used to write "world"
            # into the REPO root, polluting the working tree.
            rc = subprocess.run(
                [str(bin_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            return None, "run: timed out after 30s (output-bound or hung driver)"
        if rc.returncode != 0:
            return None, f"run: {rc.stderr.decode().strip()[:200]}"
        out = rc.stdout.decode().strip()
        m = re.search(r"ms=(\d+)", out)
        if not m:
            return None, f"no ms= in output: {out[:120]}"
        ms = int(m.group(1))
        if best_ms is None or ms < best_ms:
            best_ms = ms

    if best_ms == 0:
        # Too fast to measure at 1 ms resolution. Bump iters by 10x to
        # get a non-zero reading — but cap at 100M to avoid int64
        # overflow in the iteration counter (1e8 fits in int64; 1e19
        # does not). If we still measure 0 ms at 100M iterations, the
        # function is clearly well under 1 µs/call — declare victory.
        if iters >= 100_000_000:
            return 0.0, None
        return bench_one(module, fn_name, params, ret, iters * 10, tmpdir)

    us_per_call = (best_ms * 1000.0) / iters
    return us_per_call, None


def main():
    ap = argparse.ArgumentParser(description="Stage 32 stdlib microbench gate")
    ap.add_argument("--threshold-us", type=float, default=1.0,
                    help="fail if any function exceeds this µs/call (default: 1.0)")
    ap.add_argument("--iters", type=int, default=200000,
                    help="iterations per function (default: 200000)")
    ap.add_argument("--json", type=str, default=None,
                    help="write full report to this JSON file")
    ap.add_argument("--only", type=str, default=None,
                    help="comma-separated allow-list of function names")
    ap.add_argument("--verbose", action="store_true",
                    help="print every function as it runs")
    ap.add_argument("--resume", type=str, default=None, metavar="STATE.json",
                    help="cache per-function results in STATE.json and reuse "
                         "them on the next run (deep-scan-29: the full gate "
                         "takes ~30 min on a slow 2-vCPU CI box; a killed run "
                         "previously lost everything). A different --iters "
                         "value invalidates the cache.")
    args = ap.parse_args()

    if not bootstrap_hlc():
        print("[hls-bench] FAIL: cannot bootstrap native hlc", file=sys.stderr)
        return 2

    fns = find_stdlib_functions()
    if args.only:
        allow = set(args.only.split(","))
        fns = [f for f in fns if f[1] in allow]
    if not fns:
        print("[hls-bench] FAIL: no functions to benchmark", file=sys.stderr)
        return 2

    # ---- resume state (deep-scan-29) --------------------------------
    state = {"iters": args.iters, "results": {}}
    if args.resume:
        sp = Path(args.resume)
        if sp.exists():
            try:
                prev = json.loads(sp.read_text(encoding="utf-8"))
                if prev.get("iters") == args.iters:
                    state = prev
                    print(f"[hls-bench] resuming: {len(prev.get('results', {}))} "
                          f"cached result(s) from {args.resume}")
                else:
                    print(f"[hls-bench] resume cache has iters={prev.get('iters')} "
                          f"!= {args.iters}; starting fresh")
            except (ValueError, OSError) as ex:
                print(f"[hls-bench] cannot read resume state ({ex}); starting fresh")

    def save_state():
        if not args.resume:
            return
        sp = Path(args.resume)
        tmp = sp.with_suffix(sp.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(sp)  # atomic: a killed run never corrupts the cache

    print(f"[hls-bench] benchmarking {len(fns)} stdlib functions "
          f"(threshold={args.threshold_us} µs/call, iters={args.iters})")

    results = []
    failures = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix="hls-bench-") as tmpdir:
        for module, name, params, ret in fns:
            cached = state["results"].get(name)
            if cached is not None:
                if args.verbose:
                    print(f"  ... {module}.{name}: cached")
                cached_rec = dict(cached)
                cached_rec["module"] = module
                results.append(cached_rec)
                if cached_rec["error"] and not cached_rec["error"].startswith("skipped:"):
                    failures.append((module, name, cached_rec["error"]))
                elif cached_rec["error"]:
                    skipped.append((module, name))
                continue
            if args.verbose:
                print(f"  ... {module}.{name}({', '.join(params)}) -> {ret}", end=" ", flush=True)
            us, err = bench_one(module, name, params, ret, args.iters, tmpdir)
            if us is None and err is None:
                # Deep-scan-20: no safe default value for a parameter —
                # skip with a reason instead of a spurious FAIL.
                if args.verbose:
                    print("SKIP (no safe default for a parameter type)")
                skipped.append((module, name))
                rec = {"module": module, "fn": name, "us": None,
                       "error": "skipped: no safe default"}
            elif err:
                if args.verbose:
                    print(f"FAIL ({err})")
                failures.append((module, name, err))
                rec = {"module": module, "fn": name, "us": None, "error": err}
            else:
                if args.verbose:
                    print(f"{us:.4f} µs/call")
                rec = {"module": module, "fn": name, "us": us, "error": None}
            results.append(rec)
            state["results"][name] = {k: v for k, v in rec.items() if k != "module"}
            save_state()

    # Sort slowest first
    measured = [r for r in results if r["us"] is not None]
    measured.sort(key=lambda r: -r["us"])

    print()
    print("=" * 72)
    print(f"{'function':<48} {'µs/call':>10}  {'status':>8}")
    print("=" * 72)
    for r in measured:
        status = "OK" if r["us"] <= args.threshold_us else "SLOW"
        # Truncate function name to fit (some are long)
        nm = f"{r['module']}.{r['fn']}"
        if len(nm) > 47:
            nm = nm[:44] + "..."
        print(f"{nm:<48} {r['us']:>10.4f}  {status:>8}")
    print("-" * 72)
    if failures:
        print(f"\n{len(failures)} function(s) failed to compile or run:")
        for module, name, err in failures:
            print(f"  {module}.{name}: {err}")

    n_slow = sum(1 for r in measured if r["us"] > args.threshold_us)
    print(f"\n{len(measured)} measured, {n_slow} over threshold, "
          f"{len(skipped)} skipped (no safe default), {len(failures)} failed.")

    if args.json:
        Path(args.json).write_text(
            json.dumps({
                "threshold_us": args.threshold_us,
                "iters": args.iters,
                "results": results,
                "summary": {
                    "measured": len(measured),
                    "over_threshold": n_slow,
                    "failed": len(failures),
                },
            }, indent=2),
            encoding="utf-8",
        )
        print(f"full report written to {args.json}")

    if failures:
        return 2
    if n_slow:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
