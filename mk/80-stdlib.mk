# ============================================================================
# Stage 39 (v0.58.0-alpha): std.http2 -- HTTP/2 + ALPN negotiation
# ----------------------------------------------------------------------------
# Pure-HLS implementation of HTTP/2 (RFC 7540) + HPACK (RFC 7541),
# layered on Stage 37's std.net for the TCP transport and Stage 38's
# std.http for the HttpHeader struct. No new compiler builtins; no
# real network I/O exercised by the acceptance test (the framing layer,
# HPACK, the stream state machine, flow control accounting, server
# push, and ALPN negotiation are all verified in isolation against
# synthetic byte streams).
# ============================================================================
http2-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/http2_demo.hls > /tmp/http2_interp.txt 2>&1
	@bin/hlc examples/http2_demo.hls /tmp/http2_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/http2_demo /tmp/http2_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/http2_demo > /tmp/http2_nat.txt 2>&1
	@diff -q /tmp/http2_interp.txt /tmp/http2_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: http2_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage39_http2.hls > /tmp/s39_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage39_http2.hls /tmp/s39.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s39 /tmp/s39.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s39 > /tmp/s39_nat.txt 2>&1
	@diff -q /tmp/s39_interp.txt /tmp/s39_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage39_http2 differential mismatch" && false)
	@rm -f /tmp/http2_demo /tmp/http2_demo.c /tmp/http2_interp.txt /tmp/http2_nat.txt \
		/tmp/s39 /tmp/s39.c /tmp/s39_interp.txt /tmp/s39_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 39 -- std.http2 (HTTP/2 + ALPN negotiation)"
	@echo "  Http2Frame struct + 9-byte header encode/decode (24-bit length, 31-bit stream id)"
	@echo "  Frame parser + serialiser (all 10 frame types: DATA/HEADERS/PRIORITY/RST_STREAM/SETTINGS/PUSH_PROMISE/PING/GOAWAY/WINDOW_UPDATE/CONTINUATION)"
	@echo "  HPACK static table (61 entries, RFC 7541 Appendix A) + dynamic table (size-bounded eviction)"
	@echo "  HPACK integer decoder (RFC 7541 section 5.1) + literal string decoder (Huffman deferred)"
	@echo "  HPACK header block encoder (literal, no indexing) + decoder (all 4 representations)"
	@echo "  SETTINGS exchange + ACK + validation (ENABLE_PUSH, MAX_FRAME_SIZE bounds)"
	@echo "  Connection preface verification (24-byte magic)"
	@echo "  Stream state machine (idle/open/half-closed/closed)"
	@echo "  Flow control accounting (per-stream + per-connection windows)"
	@echo "  Server push (PUSH_PROMISE frame + header block)"
	@echo "  ALPN negotiation (h2 selection from offered list)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: http2-acceptance

# ============================================================================
# Stage 40 (v0.59.0-alpha): std.json_stream -- streaming JSON parser
# ----------------------------------------------------------------------------
# Pure-HLS constant-memory streaming JSON parser. JsonReader produces
# one token at a time and never holds the whole document in memory.
# Useful for parsing multi-GB JSON logs, NDJSON streams, or any JSON
# source that's too large to fit in RAM. The existing std.json
# document parser (Stage 10-beta, v0.6.0-alpha) remains for small
# inputs.
# ============================================================================
json-stream-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/json_stream_demo.hls > /tmp/js_interp.txt 2>&1
	@bin/hlc examples/json_stream_demo.hls /tmp/js_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/js_demo /tmp/js_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/js_demo > /tmp/js_nat.txt 2>&1
	@diff -q /tmp/js_interp.txt /tmp/js_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: json_stream_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage40_json_stream.hls > /tmp/s40_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage40_json_stream.hls /tmp/s40.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s40 /tmp/s40.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s40 > /tmp/s40_nat.txt 2>&1
	@diff -q /tmp/s40_interp.txt /tmp/s40_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage40_json_stream differential mismatch" && false)
	@rm -f /tmp/js_demo /tmp/js_demo.c /tmp/js_interp.txt /tmp/js_nat.txt \
		/tmp/s40 /tmp/s40.c /tmp/s40_interp.txt /tmp/s40_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 40 -- std.json_stream (streaming JSON parser, constant-memory)"
	@echo "  JsonReader struct + feed/finish/next_token API"
	@echo "  JsonToken enum (10 variants: ObjectStart/End, ArrayStart/End, Key, String, Int, Float, Bool, Null)"
	@echo "  Incremental parse (byte-by-byte feed, 1/4/16/64/256-byte chunks)"
	@echo "  State machine (TOP, OBJECT_KEY, OBJECT_COLON, OBJECT_VAL, ARRAY_VAL, DONE)"
	@echo "  String escapes (n t r quote backslash slash b f uXXXX — all decoded)"
	@echo "  Surrogate pair decoding (\\uD83D\\uDE00 -> 4-byte UTF-8 [0xF0, 0x9F, 0x98, 0x80])"
	@echo "  Number edge cases (int, float, negative, exponent)"
	@echo "  Integer range check (INT64_MAX/MIN accepted; out-of-range rejected)"
	@echo "  Empty containers, trailing data, empty input, unbalanced containers"
	@echo "  Invalid escapes, max_token_size limit, nesting depth limit"
	@echo "  NDJSON stream (3 records, 24 tokens)"
	@echo "  Large document (1000-element array, 4002 tokens, 256-byte chunks)"
	@echo "  Token-kind predicates (10 predicates verified)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: json-stream-acceptance

# ============================================================================
# Stage 41 (v0.60.0-alpha): std.regex -- NFA-based regular expressions
# ----------------------------------------------------------------------------
# Pure-HLS NFA-based regex engine using Thompson's construction.
# Non-backtracking -- guarantees O(n*m) worst-case time, no ReDoS.
# No new compiler builtins.
# ============================================================================
regex-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/regex_demo.hls > /tmp/re_interp.txt 2>&1
	@bin/hlc examples/regex_demo.hls /tmp/re_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/re_demo /tmp/re_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/re_demo > /tmp/re_nat.txt 2>&1
	@diff -q /tmp/re_interp.txt /tmp/re_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: regex_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage41_regex.hls > /tmp/s41_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage41_regex.hls /tmp/s41.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s41 /tmp/s41.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s41 > /tmp/s41_nat.txt 2>&1
	@diff -q /tmp/s41_interp.txt /tmp/s41_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage41_regex differential mismatch" && false)
	@rm -f /tmp/re_demo /tmp/re_demo.c /tmp/re_interp.txt /tmp/re_nat.txt \
		/tmp/s41 /tmp/s41.c /tmp/s41_interp.txt /tmp/s41_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 41 -- std.regex (NFA-based regex, no ReDoS)"
	@echo "  Regex struct + RegexState + RegexCharClass + RegexMatch"
	@echo "  RegexParser (recursive descent) + RegexFrag (Thompson construction)"
	@echo "  Quantifiers: * + ? {n} {n,} {n,m} {,m}"
	@echo "  Alternation: | (capture groups, non-capturing (?:...))"
	@echo "  Character classes: [a-z] [^0-9] [\\d \\w \\s]"
	@echo "  Anchors: ^ $ \\b \\B (zero-width)"
	@echo "  Escapes: \\n \\t \\r \\f \\v \\a \\0 \\xNN \\x{NNNN}"
	@echo "  API: compile/is_match/find/find_at/find_all/groups/replace/replace_all/split"
	@echo "  ReDoS safety: (a+)+b on 50 a's completes in microseconds"
	@echo "  differential (interpreter == native) verified green."

.PHONY: regex-acceptance

# ============================================================================
# Stage 42 (v0.61.0-alpha): std.fmt -- Display / Debug traits + format!
# ----------------------------------------------------------------------------
# Pure-HLS Display / Debug formatters and a Rust-style format-string
# interpreter. Monomorphic helpers per concrete type (matching the
# std.io convention); FmtValue enum for heterogeneous format args.
# No new compiler builtins.
# ============================================================================
fmt-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/fmt_demo.hls > /tmp/fmt_interp.txt 2>&1
	@bin/hlc examples/fmt_demo.hls /tmp/fmt_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/fmt_demo /tmp/fmt_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/fmt_demo > /tmp/fmt_nat.txt 2>&1
	@diff -q /tmp/fmt_interp.txt /tmp/fmt_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: fmt_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage42_fmt.hls > /tmp/s42_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage42_fmt.hls /tmp/s42.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s42 /tmp/s42.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s42 > /tmp/s42_nat.txt 2>&1
	@diff -q /tmp/s42_interp.txt /tmp/s42_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage42_fmt differential mismatch" && false)
	@rm -f /tmp/fmt_demo /tmp/fmt_demo.c /tmp/fmt_interp.txt /tmp/fmt_nat.txt \
		/tmp/s42 /tmp/s42.c /tmp/s42_interp.txt /tmp/s42_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 42 -- std.fmt (Display / Debug traits + format!)"
	@echo "  Display formatters (int, str, bool, float -- %.6f form)"
	@echo "  Debug formatters (int, bool, float; str with escapes \\\\n \\\\t \\\\r \\\\\\\\ \\\\\" \\\\0 \\\\xNN)"
	@echo "  FmtValue enum (Int/Float/Str/Bool) for heterogeneous format args"
	@echo "  fmt(format, args) -> Result[str, str] interpreter"
	@echo "  Placeholder grammar: {} {N} {:spec} {N:spec} {{ }}"
	@echo "  Specifiers: {:?} {:b} {:o} {:x} {:X} {:e} {:>N} {:<N} {:^N} {:0N} {:.P}"
	@echo "  Width + precision combinations; literal braces; error handling"
	@echo "  Number-base helpers (binary, octal, hex lower/upper)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: fmt-acceptance

# ============================================================================
# Stage 43 (v0.62.0-alpha): std.hash -- SipHash, xxHash, FNV, CityHash
# ============================================================================
hash-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/hash_demo.hls > /tmp/hash_interp.txt 2>&1
	@bin/hlc examples/hash_demo.hls /tmp/hash_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/hash_demo /tmp/hash_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/hash_demo > /tmp/hash_nat.txt 2>&1
	@diff -q /tmp/hash_interp.txt /tmp/hash_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: hash_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage43_hash.hls > /tmp/s43_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage43_hash.hls /tmp/s43.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s43 /tmp/s43.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s43 > /tmp/s43_nat.txt 2>&1
	@diff -q /tmp/s43_interp.txt /tmp/s43_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage43_hash differential mismatch" && false)
	@rm -f /tmp/hash_demo /tmp/hash_demo.c /tmp/hash_interp.txt /tmp/hash_nat.txt \
		/tmp/s43 /tmp/s43.c /tmp/s43_interp.txt /tmp/s43_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 43 -- std.hash (SipHash, xxHash, FNV, CityHash)"
	@echo "  SipHash-2-4 (DoS-resistant, keyed, official test vector verified)"
	@echo "  xxHash64 (fast non-crypto, official test vector verified)"
	@echo "  FNV-1a 64-bit (small code, official test vector verified)"
	@echo "  CityHash64 (tuned for short strings)"
	@echo "  Hash trait helpers (hash_int/str/bool/float/combine)"
	@echo "  Uniform Hasher enum API"
	@echo "  differential (interpreter == native) verified green."

.PHONY: hash-acceptance

# ============================================================================
# Stage 44 (v0.63.0-alpha): std.collections -- BTreeMap, HashSet, LinkedList, RingBuf, HashMap
# ============================================================================
collections-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/collections_demo.hls > /tmp/coll_interp.txt 2>&1
	@bin/hlc examples/collections_demo.hls /tmp/coll_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/coll_demo /tmp/coll_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/coll_demo > /tmp/coll_nat.txt 2>&1
	@diff -q /tmp/coll_interp.txt /tmp/coll_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: collections_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage44_collections.hls > /tmp/s44_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage44_collections.hls /tmp/s44.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s44 /tmp/s44.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s44 > /tmp/s44_nat.txt 2>&1
	@diff -q /tmp/s44_interp.txt /tmp/s44_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage44_collections differential mismatch" && false)
	@rm -f /tmp/coll_demo /tmp/coll_demo.c /tmp/coll_interp.txt /tmp/coll_nat.txt \
		/tmp/s44 /tmp/s44.c /tmp/s44_interp.txt /tmp/s44_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 44 -- std.collections (BTreeMap, HashSet, LinkedList, RingBuf, HashMap)"
	@echo "  BTreeMap (sorted, O(log n) binary search lookup, min/max)"
	@echo "  HashSet[int/str] (union, intersect, diff, equal)"
	@echo "  LinkedList[int/str] (O(1) push/pop both ends, arena-based)"
	@echo "  RingBuf[int/str] (fixed-capacity, overwrite mode)"
	@echo "  HashMap[int->str/int] (SipHash-keyed, collision handling)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: collections-acceptance

# ============================================================================
# Stage 45 (v0.64.0-alpha): std.sync -- Mutex, RwLock, Condvar, OnceCell, Barrier
# ============================================================================
sync-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/sync_demo.hls > /tmp/sync_interp.txt 2>&1
	@bin/hlc examples/sync_demo.hls /tmp/sync_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/sync_demo /tmp/sync_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/sync_demo > /tmp/sync_nat.txt 2>&1
	@diff -q /tmp/sync_interp.txt /tmp/sync_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: sync_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage45_sync.hls > /tmp/s45_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage45_sync.hls /tmp/s45.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s45 /tmp/s45.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s45 > /tmp/s45_nat.txt 2>&1
	@diff -q /tmp/s45_interp.txt /tmp/s45_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage45_sync differential mismatch" && false)
	@# Stage 45 acceptance: hlmodel verifies the 2-thread / 2-lock
	@# acquisition protocol is deadlock-free (Deadlock state unreachable).
	@python3 tools/hlmodel.py tests/ok/feat_stage45_sync_model.hls --fn step --invariant no_deadlock --init Init > /tmp/s45_hlmodel.txt 2>&1
	@grep -q "invariant 'no_deadlock' holds on every reachable state" /tmp/s45_hlmodel.txt \
		|| (echo "FAIL: hlmodel deadlock check failed" && cat /tmp/s45_hlmodel.txt && false)
	@rm -f /tmp/sync_demo /tmp/sync_demo.c /tmp/sync_interp.txt /tmp/sync_nat.txt \
		/tmp/s45 /tmp/s45.c /tmp/s45_interp.txt /tmp/s45_nat.txt /tmp/s45_hlmodel.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 45 -- std.sync (Mutex, RwLock, Condvar, OnceCell, Barrier)"
	@echo "  Mutex (cap-1 token channel, try_lock via recv_or)"
	@echo "  RwLock (writer + count_mu + reader_count channels, no hold-and-wait)"
	@echo "  Condvar (cap-1024 signal channel, broadcast wakes N waiters)"
	@echo "  OnceCell[int/str/bool] (state + value channels, race-to-init)"
	@echo "  Barrier (single-use, n-1 release tokens from last arrival)"
	@echo "  hlmodel: 2-thread / 2-lock deadlock-free (Deadlock unreachable)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: sync-acceptance

# ============================================================================
# Stage 46 (v0.65.0-alpha): std.thread -- thread_sleep_ms, thread_yield,
# thread_current_id, ThreadBuilder. Three new compiler builtins wired
# through all four code-paths (boot checker, boot interpreter,
# self-hosted compiler C codegen + C runtime, LLVM IR emit).
# ============================================================================
thread-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/thread_demo.hls > /tmp/thread_interp.txt 2>&1
	@bin/hlc examples/thread_demo.hls /tmp/thread_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/thread_demo /tmp/thread_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/thread_demo > /tmp/thread_nat.txt 2>&1
	@diff -q /tmp/thread_interp.txt /tmp/thread_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: thread_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage46_thread.hls > /tmp/s46_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage46_thread.hls /tmp/s46.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s46 /tmp/s46.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s46 > /tmp/s46_nat.txt 2>&1
	@diff -q /tmp/s46_interp.txt /tmp/s46_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage46_thread differential mismatch" && false)
	@rm -f /tmp/thread_demo /tmp/thread_demo.c /tmp/thread_interp.txt /tmp/thread_nat.txt \
		/tmp/s46 /tmp/s46.c /tmp/s46_interp.txt /tmp/s46_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 46 -- std.thread (sleep, yield, current_id, Builder)"
	@echo "  thread_sleep_ms (nanosleep on POSIX, time.sleep in interpreter; Clock + Conc)"
	@echo "  thread_yield (sched_yield on POSIX, time.sleep(0) in interpreter; Conc)"
	@echo "  thread_current_id (pthread_self cast to int64; non-zero, distinct per thread)"
	@echo "  ThreadBuilder (name + stack_size; immutable update pattern)"
	@echo "  three new builtins wired through all 4 code-paths (checker/interp/hlc/llvm)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: thread-acceptance

# ============================================================================
# Stage 47 (v0.66.0-alpha): std.process -- proc_spawn, proc_wait,
# proc_kill, proc_child_write, proc_child_read, proc_child_close.
# Six new compiler builtins wired through all four code-paths
# (boot checker, boot interpreter, self-hosted compiler C codegen +
# C runtime, LLVM IR emit). Command / Child / ExitStatus / Stdio
# types and helpers in std/process.hls.
# ============================================================================
process-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/process_demo.hls > /tmp/process_interp.txt 2>&1
	@bin/hlc examples/process_demo.hls /tmp/process_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/process_demo /tmp/process_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/process_demo > /tmp/process_nat.txt 2>&1
	@diff -q /tmp/process_interp.txt /tmp/process_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: process_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage47_process.hls > /tmp/s47_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage47_process.hls /tmp/s47.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s47 /tmp/s47.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s47 > /tmp/s47_nat.txt 2>&1
	@diff -q /tmp/s47_interp.txt /tmp/s47_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage47_process differential mismatch" && false)
	@# Stage 47: taint-sink enforcement -- a tainted program is rejected.
	@if $(PYTHON) boot/boot.py --check tests/fail/fail_taint_proc_spawn.hls >/dev/null 2>&1; then \
		echo "FAIL: fail_taint_proc_spawn should have been rejected"; false; \
	fi
	@if $(PYTHON) boot/boot.py --check tests/fail/fail_effect_proc_spawn_missing.hls >/dev/null 2>&1; then \
		echo "FAIL: fail_effect_proc_spawn_missing should have been rejected"; false; \
	fi
	@rm -f /tmp/process_demo /tmp/process_demo.c /tmp/process_interp.txt /tmp/process_nat.txt \
		/tmp/s47 /tmp/s47.c /tmp/s47_interp.txt /tmp/s47_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 47 -- std.process (Command, Child, ExitStatus, Stdio)"
	@echo "  proc_spawn (fork+execvp on POSIX, subprocess.Popen in interpreter; Proc)"
	@echo "  proc_wait (waitpid; 0..255 exit / 128+signum signal / -1 error)"
	@echo "  proc_kill (SIGTERM; idempotent on already-exited children)"
	@echo "  proc_child_write / proc_child_read / proc_child_close (pipe I/O)"
	@echo "  Command builder + Child handle + ExitStatus decoder + Stdio enum"
	@echo "  proc_spawn program arg is a taint sink (command injection)"
	@echo "  six new builtins wired through all 4 code-paths (checker/interp/hlc/llvm)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: process-acceptance

# ============================================================================
# Stage 48 (v0.67.0-alpha): std.env -- env_get, env_has, env_set,
# env_unset, cwd_get, cwd_set, args_os. Seven new compiler builtins
# wired through all four code-paths (boot checker, boot interpreter,
# self-hosted compiler C codegen + C runtime, LLVM IR emit).
# std/env.hls provides the user-facing API: env_var (Option[tainted[str]]),
# env_set_var, env_unset_var, env_current_dir (tainted[str]),
# env_set_current_dir, env_args_os (list[tainted[str]]).
# ============================================================================
env-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/env_demo.hls > /tmp/env_interp.txt 2>&1
	@bin/hlc examples/env_demo.hls /tmp/env_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/env_demo /tmp/env_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/env_demo > /tmp/env_nat.txt 2>&1
	@diff -q /tmp/env_interp.txt /tmp/env_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: env_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage48_env.hls > /tmp/s48_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage48_env.hls /tmp/s48.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s48 /tmp/s48.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s48 > /tmp/s48_nat.txt 2>&1
	@diff -q /tmp/s48_interp.txt /tmp/s48_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage48_env differential mismatch" && false)
	@# Stage 48: taint-sink enforcement -- tainted keys and paths are rejected.
	@if $(PYTHON) boot/boot.py --check tests/fail/fail_taint_env_get.hls >/dev/null 2>&1; then \
		echo "FAIL: fail_taint_env_get should have been rejected"; false; \
	fi
	@if $(PYTHON) boot/boot.py --check tests/fail/fail_taint_env_set.hls >/dev/null 2>&1; then \
		echo "FAIL: fail_taint_env_set should have been rejected"; false; \
	fi
	@if $(PYTHON) boot/boot.py --check tests/fail/fail_taint_env_set_var.hls >/dev/null 2>&1; then \
		echo "FAIL: fail_taint_env_set_var should have been rejected"; false; \
	fi
	@if $(PYTHON) boot/boot.py --check tests/fail/fail_taint_cwd_set.hls >/dev/null 2>&1; then \
		echo "FAIL: fail_taint_cwd_set should have been rejected"; false; \
	fi
	@rm -f /tmp/env_demo /tmp/env_demo.c /tmp/env_interp.txt /tmp/env_nat.txt \
		/tmp/s48 /tmp/s48.c /tmp/s48_interp.txt /tmp/s48_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 48 -- std.env (env_var, env_set_var, env_unset_var, env_current_dir, env_set_current_dir, env_args_os)"
	@echo "  env_get / env_has / env_set / env_unset (low-level env builtins; Proc)"
	@echo "  cwd_get / cwd_set (low-level cwd builtins; Proc)"
	@echo "  args_os (low-level argv builtin; same as args() at runtime)"
	@echo "  env_var (stdlib: Option[tainted[str]] built from env_has + env_get + taint_mark)"
	@echo "  env_set_var / env_unset_var (stdlib: thin wrappers)"
	@echo "  env_current_dir (stdlib: tainted[str] wraps cwd_get with taint_mark)"
	@echo "  env_set_current_dir (stdlib: thin wrapper; returns 0 / -1)"
	@echo "  env_args_os (stdlib: list[tainted[str]] wraps args_os + taint_mark per element)"
	@echo "  taint sinks: env_get/env_has/env_set/env_unset key, cwd_set path"
	@echo "  seven new builtins wired through all 4 code-paths (checker/interp/hlc/llvm)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: env-acceptance

# ============================================================================
# Stage 49 (v0.68.0-alpha): std.time -- Instant, Duration, SystemTime,
# sleep, timeout. Two new compiler builtins (instant_now_ns,
# system_time_now_ms) wired through all four code-paths.
# ============================================================================

# ============================================================================
# Stage 50 (v0.69.0-alpha): std.math -- IEEE-754 + libm-backed
# transcendental + special functions + BigDecimal. 28 new builtins.
# ============================================================================
math-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/math_demo.hls > /tmp/math_interp.txt 2>&1
	@bin/hlc examples/math_demo.hls /tmp/math_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/math_demo /tmp/math_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/math_demo > /tmp/math_nat.txt 2>&1
	@diff -q /tmp/math_interp.txt /tmp/math_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: math_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage50_math.hls > /tmp/s50_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage50_math.hls /tmp/s50.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s50 /tmp/s50.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s50 > /tmp/s50_nat.txt 2>&1
	@diff -q /tmp/s50_interp.txt /tmp/s50_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage50_math differential mismatch" && false)
	@rm -f /tmp/math_demo /tmp/math_demo.c /tmp/math_interp.txt /tmp/math_nat.txt \
		/tmp/s50 /tmp/s50.c /tmp/s50_interp.txt /tmp/s50_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 50 -- std.math (IEEE-754 + transcendental + BigDecimal)"
	@echo "  IEEE-754 predicates: math_isnan / isinf / isfinite / signbit"
	@echo "  Trig: math_sin / cos / tan / asin / acos / atan / atan2 / sinh / cosh / tanh"
	@echo "  Exp/log: math_exp / log / log10 / log2 / pow"
	@echo "  Power/root: math_sqrt / cbrt / hypot / fmod / copysign"
	@echo "  Special: math_erf / erfc / tgamma / lgamma"
	@echo "  Constants: math_pi / math_e / math_pos_inf / math_neg_inf / math_nan"
	@echo "  BigDecimal (pure HLS): from_int / from_str / to_str / to_int /"
	@echo "    add / sub / mul / neg / abs / eq / lt / gt / le / ge / is_zero"
	@echo "  28 new builtins wired through all 4 code-paths (checker/interp/hlc/llvm)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: math-acceptance

# ============================================================================
# Stage 49 (v0.68.0-alpha): std.time -- Instant, Duration, SystemTime,
# sleep, timeout. Two new compiler builtins (instant_now_ns,
# system_time_now_ms) wired through all four code-paths.
#
# Note: time_demo.hls prints actual timestamps (non-deterministic) so
# it is excluded from the differential byte-comparison. The acceptance
# test uses feat_stage49_time.hls (deterministic — only prints section
# markers and "ok"). The demo is invoked to verify it RUNS cleanly
# (exit 0) under both interpreter and native.
# ============================================================================
time-acceptance: bin/hlc
	@# Verify time_demo runs cleanly under both paths (exit 0).
	@$(PYTHON) boot/boot.py examples/time_demo.hls > /tmp/time_demo_interp.txt 2>&1
	@bin/hlc examples/time_demo.hls /tmp/time_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/time_demo /tmp/time_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/time_demo > /tmp/time_demo_nat.txt 2>&1
	@# Both must end with the ACCEPTANCE OK line (verifies they ran to completion).
	@tail -n 1 /tmp/time_demo_interp.txt | grep -q "ACCEPTANCE OK" \
		|| (echo "FAIL: time_demo interpreter did not reach ACCEPTANCE OK" && false)
	@tail -n 1 /tmp/time_demo_nat.txt | grep -q "ACCEPTANCE OK" \
		|| (echo "FAIL: time_demo native did not reach ACCEPTANCE OK" && false)
	@# The deterministic differential is on feat_stage49_time.hls.
	@$(PYTHON) boot/boot.py tests/ok/feat_stage49_time.hls > /tmp/s49_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage49_time.hls /tmp/s49.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s49 /tmp/s49.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s49 > /tmp/s49_nat.txt 2>&1
	@diff -q /tmp/s49_interp.txt /tmp/s49_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage49_time differential mismatch" && false)
	@rm -f /tmp/time_demo /tmp/time_demo.c /tmp/time_demo_interp.txt /tmp/time_demo_nat.txt \
		/tmp/s49 /tmp/s49.c /tmp/s49_interp.txt /tmp/s49_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 49 -- std.time (Instant, Duration, SystemTime, sleep, timeout)"
	@echo "  instant_now_ns (monotonic ns; Clock) + system_time_now_ms (wall-clock ms; Clock)"
	@echo "  Instant struct: now / elapsed / duration_since / eq / lt / add(Duration) / sub(Duration)"
	@echo "  Duration struct: from_secs / mins / millis / micros / nanos"
	@echo "  Duration accessors: as_nanos / as_micros / as_millis / as_secs / as_mins / as_hours / as_days"
	@echo "  Duration subsec: subsec_nanos / subsec_millis / subsec_micros"
	@echo "  Duration checked arithmetic: add / sub / mul / div (panics on overflow)"
	@echo "  Duration comparison: eq / lt / le / gt / ge"
	@echo "  Duration helpers: abs / is_zero / is_negative / is_positive / to_str"
	@echo "  SystemTime struct: now / unix_secs / unix_millis / to_iso8601"
	@echo "  SystemTime::duration_since -> Result[Duration, str] (Err on backwards wall clock)"
	@echo "  time_sleep(Duration) blocks the calling thread (wraps thread_sleep_ms)"
	@echo "  time_timeout_ms(ms) -> Future[bool] (race a future against a timer)"
	@echo "  two new builtins wired through all 4 code-paths (checker/interp/hlc/llvm)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: time-acceptance

# ============================================================================
# Stage 51 (v0.70.0-alpha): std.archive -- TarReader, ZipReader,
# GzipEncoder, GzipDecoder with bounded decompression (zip bomb detection).
# Pure-HLS implementation (no new compiler builtins) — uses read_file
# (Fs effect) + byte_at + int_and/or/xor/shl/shr (Stage 32).
# ============================================================================

# Stage 51 archive demo is deterministic (all archive formats are
# built in-memory — no live data). The differential test compares
# interpreter and native output byte-for-byte.
archive-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/archive_demo.hls > /tmp/arch_demo_interp.txt 2>&1
	@bin/hlc examples/archive_demo.hls /tmp/arch_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/arch_demo /tmp/arch_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/arch_demo > /tmp/arch_demo_nat.txt 2>&1
	@diff -q /tmp/arch_demo_interp.txt /tmp/arch_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: archive_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage51_archive.hls > /tmp/s51_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage51_archive.hls /tmp/s51.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s51 /tmp/s51.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s51 > /tmp/s51_nat.txt 2>&1
	@diff -q /tmp/s51_interp.txt /tmp/s51_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage51_archive differential mismatch" && false)
	@rm -f /tmp/arch_demo /tmp/arch_demo.c /tmp/arch_demo_interp.txt /tmp/arch_demo_nat.txt \
		/tmp/s51 /tmp/s51.c /tmp/s51_interp.txt /tmp/s51_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 51 -- std.archive (tar + zip + gzip)"
	@echo "  TarReader (USTAR format, typeflag '0' regular file + '5' directory)"
	@echo "  ZipReader (stored entries, method=0, CRC32 verified against central directory)"
	@echo "  GzipEncoder (RFC 1952 framing, stored DEFLATE blocks BTYPE=00)"
	@echo "  GzipDecoder (stored blocks BTYPE=00; compressed blocks BTYPE=01/10 panic)"
	@echo "  archive_crc32 (IEEE 802.3 polynomial 0xEDB88320, table-driven)"
	@echo "  Bounded decompression (configurable max_ratio, default 100; zip bomb panic)"
	@echo "  No unsafe decompression (every byte bounds-checked)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: archive-acceptance

# ============================================================================
# Stage 52 (v0.71.0-alpha): std.uuid v7 + std.ulid. UUID v7 (RFC 9562)
# is time-ordered (48-bit Unix ms + 74 random bits). ULID is 26-char
# Crockford base32 (lexicographically sortable). Both use
# system_time_now_ms (Stage 49, Clock) + rand_int (Stage 9, Rand).
# Pure-HLS implementation (no new compiler builtins).
# ============================================================================

# Stage 52 uuid_ulid demo prints LIVE UUIDs/ULIDs (current time + random),
# so it is non-deterministic. We verify it RUNS cleanly under both
# backends (exit 0, prints ACCEPTANCE OK), but do NOT compare output
# byte-for-byte (the live values differ between interpreter and native).
# The deterministic differential test uses feat_stage52_uuid_ulid.hls
# (no live-data printing in the assertions — only format / sort / round-trip
# checks, which are deterministic).
uuid-ulid-acceptance: bin/hlc
	@# Verify the demo runs cleanly under both backends (exit 0 + ACCEPTANCE OK).
	@$(PYTHON) boot/boot.py examples/uuid_ulid_demo.hls > /tmp/uuid_demo_interp.txt 2>&1
	@bin/hlc examples/uuid_ulid_demo.hls /tmp/uuid_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/uuid_demo /tmp/uuid_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/uuid_demo > /tmp/uuid_demo_nat.txt 2>&1
	@tail -n 1 /tmp/uuid_demo_interp.txt | grep -q "ACCEPTANCE OK" \
		|| (echo "FAIL: uuid_ulid_demo interpreter did not reach ACCEPTANCE OK" && false)
	@tail -n 1 /tmp/uuid_demo_nat.txt | grep -q "ACCEPTANCE OK" \
		|| (echo "FAIL: uuid_ulid_demo native did not reach ACCEPTANCE OK" && false)
	@# The deterministic differential is on feat_stage52_uuid_ulid.hls.
	@$(PYTHON) boot/boot.py tests/ok/feat_stage52_uuid_ulid.hls > /tmp/s52_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage52_uuid_ulid.hls /tmp/s52.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s52 /tmp/s52.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s52 > /tmp/s52_nat.txt 2>&1
	@diff -q /tmp/s52_interp.txt /tmp/s52_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage52_uuid_ulid differential mismatch" && false)
	@# Verify existing uuid test still passes (backwards compat).
	@$(PYTHON) boot/boot.py tests/ok/feat_stdlib_uuid.hls > /tmp/s10_uuid_interp.txt 2>&1
	@tail -n 1 /tmp/s10_uuid_interp.txt | grep -q "OK: uuid" \
		|| (echo "FAIL: feat_stdlib_uuid backwards-compat regression" && false)
	@rm -f /tmp/uuid_demo /tmp/uuid_demo.c /tmp/uuid_demo_interp.txt /tmp/uuid_demo_nat.txt \
		/tmp/s52 /tmp/s52.c /tmp/s52_interp.txt /tmp/s52_nat.txt \
		/tmp/s10_uuid_interp.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 52 -- std.uuid v7 + std.ulid"
	@echo "  UUID v7 (RFC 9562): 48-bit Unix ms timestamp + 12-bit rand_a + 62-bit rand_b"
	@echo "  UUID v7 API: uuid_v7() (live), uuid_v7_from(ts_ms, rand_a, rand_b), uuid_v7_timestamp(s)"
	@echo "  ULID: 48-bit Unix ms timestamp + 80-bit randomness, 26-char Crockford base32"
	@echo "  ULID API: ulid() (live), ulid_from(ts_ms, rand_bytes), ulid_timestamp(s),"
	@echo "    ulid_randomness(s) -> 10 bytes, ulid_is_valid(s), ulid_compare(a, b)"
	@echo "  Both use system_time_now_ms (Clock) + rand_int (Rand) — no new compiler builtins"
	@echo "  Pure-HLS implementation (extensions to std/uuid.hls; v4/v5 unchanged)"
	@echo "  ULID encoding matches python-ulid and the JS reference implementation"
	@echo "  Lexicographically sortable (the killer feature for database indexes / log correlation)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: uuid-ulid-acceptance

# ============================================================================
# Stage 53 (v0.72.0-alpha): std.cli -- type-safe CLI argument parser
# (CliParser builder, flags/int/str/float options, positionals, env-var
# fallback, subcommands, --help/--version). Pure-HLS implementation
# (no new compiler builtins) -- uses args() (Args) + env_get/env_has
# (Proc) + println (IO).
# ============================================================================

# Stage 53 cli demo is deterministic (all parse cases use synthetic
# argv lists, not the real process argv). The differential test
# compares interpreter and native output byte-for-byte.
cli-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/cli_demo.hls > /tmp/cli_demo_interp.txt 2>&1
	@bin/hlc examples/cli_demo.hls /tmp/cli_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/cli_demo /tmp/cli_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/cli_demo > /tmp/cli_demo_nat.txt 2>&1
	@diff -q /tmp/cli_demo_interp.txt /tmp/cli_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: cli_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage53_cli.hls > /tmp/s53_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage53_cli.hls /tmp/s53.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s53 /tmp/s53.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s53 > /tmp/s53_nat.txt 2>&1
	@diff -q /tmp/s53_interp.txt /tmp/s53_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage53_cli differential mismatch" && false)
	@rm -f /tmp/cli_demo /tmp/cli_demo.c /tmp/cli_demo_interp.txt /tmp/cli_demo_nat.txt \
		/tmp/s53 /tmp/s53.c /tmp/s53_interp.txt /tmp/s53_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 53 -- std.cli (type-safe argument parser)"
	@echo "  CliParser builder (flag / int_opt / str_opt / float_opt / positional)"
	@echo "  Type-safe parsing (invalid int/float/bool rejected at parse time)"
	@echo "  Long/short options (--port / -p) with =value and separate value"
	@echo "  Bool flags (--verbose / -v) with optional explicit value"
	@echo "  Defaults (applied when option not given; cli_has=false)"
	@echo "  Env-var fallback (cli_parser_env; CLI takes precedence over env)"
	@echo "  Subcommands (init / build; remaining args go to cli_remaining_args)"
	@echo "  --help / -h and --version / -V (implicit, always registered)"
	@echo "  -- end-of-options marker (rest treated as positional)"
	@echo "  Required arg validation (missing required -> error)"
	@echo "  Help text generation (cli_print_help) + version (cli_print_version)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: cli-acceptance

