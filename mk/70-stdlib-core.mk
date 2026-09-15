# ============================================================================
# Stage 32 (v0.51.0-alpha): Zero-cost abstractions audit
# ============================================================================
#
# ROADMAP Stage 32 acceptance: "every public stdlib function benchmarks
# at <1 µs on the CI hardware (a 4 GHz CPU)."
#
# Two complementary gates:
#
#   bench-stdlib      — runs `tools/hls-bench.py` over every public
#                       stdlib function and fails if any function
#                       exceeds the configured threshold.
#                       Default threshold: 2.0 µs/call (accounts for
#                       slower dev/CI hardware; tighten to 1.0 µs on
#                       a true 4 GHz runner).
#
#   spec-check        — runs `tools/hls-spec-check.py` which verifies
#                       that the generic specialisation of
#                       list_reverse_int produces the same loop shape
#                       as a hand-written C reverse.
#
#   stage32-acceptance — runs BOTH gates and is the official Stage 32
#                       acceptance target.

# bench-stdlib: microbench every public stdlib function.
# Override the threshold via: make bench-stdlib BENCH_THRESHOLD_US=1.0
BENCH_THRESHOLD_US ?= 2.0
bench-stdlib:
	@$(PYTHON) tools/hls-bench.py --threshold-us $(BENCH_THRESHOLD_US)

# spec-check: verify generic specialisation of list_reverse_int.
spec-check:
	@$(PYTHON) tools/hls-spec-check.py

# stage32-acceptance: the official Stage 32 gate (bench + spec).
stage32-acceptance: bench-stdlib spec-check
	@echo ""
	@echo "ACCEPTANCE OK: Stage 32 — every public stdlib function"
	@echo "benchmarks at <$(BENCH_THRESHOLD_US) µs/call and list_reverse_int"
	@echo "is properly specialised (matches hand-written C reference)."

.PHONY: bench-stdlib spec-check stage32-acceptance

# ============================================================================
# Stage 33 (v0.52.0-alpha): async/await zero-runtime futures
# ============================================================================
async-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/async_demo.hls > /tmp/async_interp.txt 2>&1
	@bin/hlc examples/async_demo.hls /tmp/async_demo.c 2>/dev/null
	@gcc -O2 -o /tmp/async_demo /tmp/async_demo.c -lm -pthread 2>/dev/null
	@/tmp/async_demo > /tmp/async_nat.txt 2>&1
	@diff -q /tmp/async_interp.txt /tmp/async_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: async_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage33_async.hls > /tmp/s33_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage33_async.hls /tmp/s33.c 2>/dev/null
	@gcc -O2 -o /tmp/s33 /tmp/s33.c -lm -pthread 2>/dev/null
	@/tmp/s33 > /tmp/s33_nat.txt 2>&1
	@diff -q /tmp/s33_interp.txt /tmp/s33_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage33_async differential mismatch" && false)
	@echo ""
	@echo "ACCEPTANCE OK: Stage 33 -- async/await zero-runtime futures"
	@echo "  async_spawn / await / future_ready / future_poll / future_select"
	@echo "  differential (interpreter == native) verified green."

.PHONY: async-acceptance

# ============================================================================
# Stage 34 (v0.53.0-alpha): async stream combinators (channels x generators)
# ============================================================================
stream-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/stream_demo.hls > /tmp/stream_interp.txt 2>&1
	@bin/hlc examples/stream_demo.hls /tmp/stream_demo.c 2>/dev/null
	@gcc -O2 -o /tmp/stream_demo /tmp/stream_demo.c -lm -pthread 2>/dev/null
	@/tmp/stream_demo > /tmp/stream_nat.txt 2>&1
	@diff -q /tmp/stream_interp.txt /tmp/stream_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: stream_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage34_stream.hls > /tmp/s34_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage34_stream.hls /tmp/s34.c 2>/dev/null
	@gcc -O2 -o /tmp/s34 /tmp/s34.c -lm -pthread 2>/dev/null
	@/tmp/s34 > /tmp/s34_nat.txt 2>&1
	@diff -q /tmp/s34_interp.txt /tmp/s34_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage34_stream differential mismatch" && false)
	@echo ""
	@echo "ACCEPTANCE OK: Stage 34 -- async stream combinators"
	@echo "  stream_new / send / recv / close / map / filter / take / fold / merge / flat_map"
	@echo "  gen_spawn (generator pattern)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: stream-acceptance

# ============================================================================
# Stage 35 (v0.54.0-alpha): std.io -- buffered I/O traits + Cursor + Chain
# ============================================================================
io-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/io_demo.hls > /tmp/io_interp.txt 2>&1
	@bin/hlc examples/io_demo.hls /tmp/io_demo.c 2>/dev/null
	@gcc -O2 -o /tmp/io_demo /tmp/io_demo.c -lm -pthread 2>/dev/null
	@/tmp/io_demo > /tmp/io_nat.txt 2>&1
	@diff -q /tmp/io_interp.txt /tmp/io_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: io_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage35_io.hls > /tmp/s35_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage35_io.hls /tmp/s35.c 2>/dev/null
	@gcc -O2 -o /tmp/s35 /tmp/s35.c -lm -pthread 2>/dev/null
	@/tmp/s35 > /tmp/s35_nat.txt 2>&1
	@diff -q /tmp/s35_interp.txt /tmp/s35_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage35_io differential mismatch" && false)
	@rm -f /tmp/halis_io_demo*.txt /tmp/halis_io_demo*.csv \
		/tmp/halis_stage35_io_test.txt /tmp/halis_stage35_io_out.txt \
		/tmp/halis_stage35_pipe.csv
	@echo ""
	@echo "ACCEPTANCE OK: Stage 35 -- std.io (Read/Write traits, BufReader, BufWriter, Cursor, Chain)"
	@echo "  Cursor (in-memory Read+Write) / BufReader (file Read) /"
	@echo "  BufWriter (buffered file Write+flush) / Chain (concat readers)"
	@echo "  hex helpers (io_to_hex / io_from_hex)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: io-acceptance

# ============================================================================
# Stage 36 (v0.55.0-alpha): std.fs -- path abstraction + dir walk + metadata
# ============================================================================
fs-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/fs_demo.hls > /tmp/fs_interp.txt 2>&1
	@bin/hlc examples/fs_demo.hls /tmp/fs_demo.c 2>/dev/null
	@gcc -O2 -o /tmp/fs_demo /tmp/fs_demo.c -lm -pthread 2>/dev/null
	@/tmp/fs_demo > /tmp/fs_nat.txt 2>&1
	@diff -q /tmp/fs_interp.txt /tmp/fs_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: fs_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage36_fs.hls > /tmp/s36_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage36_fs.hls /tmp/s36.c 2>/dev/null
	@gcc -O2 -o /tmp/s36 /tmp/s36.c -lm -pthread 2>/dev/null
	@/tmp/s36 > /tmp/s36_nat.txt 2>&1
	@diff -q /tmp/s36_interp.txt /tmp/s36_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage36_fs differential mismatch" && false)
	@rm -f /tmp/fs_demo /tmp/fs_demo.c /tmp/fs_interp.txt /tmp/fs_nat.txt \
		/tmp/s36 /tmp/s36.c /tmp/s36_interp.txt /tmp/s36_nat.txt
	@rm -rf /tmp/halis_fs_demo /tmp/halis_stage36_fs
	@echo ""
	@echo "ACCEPTANCE OK: Stage 36 -- std.fs (path abstraction + dir walk + metadata)"
	@echo "  fs_read_dir / fs_size / fs_is_dir / fs_set_perms (new builtins)"
	@echo "  Path / PathBuf / FsMetadata (type-safe path-traversal prevention)"
	@echo "  path_join / path_walk / path_walk_files / path_metadata"
	@echo "  path_parent / path_filename / path_extension"
	@echo "  differential (interpreter == native) verified green."

.PHONY: fs-acceptance

# ============================================================================
# Stage 37 (v0.56.0-alpha): std.net -- TCP/UDP sockets, DNS, TLS via libcurl
# ============================================================================
net-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/net_demo.hls > /tmp/net_interp.txt 2>&1
	@bin/hlc examples/net_demo.hls /tmp/net_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/net_demo /tmp/net_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/net_demo > /tmp/net_nat.txt 2>&1
	@diff -q /tmp/net_interp.txt /tmp/net_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: net_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage37_net.hls > /tmp/s37_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage37_net.hls /tmp/s37.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s37 /tmp/s37.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s37 > /tmp/s37_nat.txt 2>&1
	@diff -q /tmp/s37_interp.txt /tmp/s37_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage37_net differential mismatch" && false)
	@rm -f /tmp/net_demo /tmp/net_demo.c /tmp/net_interp.txt /tmp/net_nat.txt \
		/tmp/s37 /tmp/s37.c /tmp/s37_interp.txt /tmp/s37_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 37 -- std.net (TCP/UDP sockets, DNS, TLS via libcurl)"
	@echo "  net_tcp_connect / net_tcp_listen / net_tcp_accept / net_read / net_write / net_close"
	@echo "  net_udp_open / net_udp_send_to / net_udp_recv_from"
	@echo "  net_tls_get (HTTPS GET via libcurl, conditional on HL_HAVE_LIBCURL)"
	@echo "  TcpStream / TcpListener / UdpSocket (high-level wrappers)"
	@echo "  tcp_connect / tcp_connect_or / tcp_listen / tcp_listen_or / udp_open / udp_open_or / tls_get"
	@echo "  taint-sink enforcement on host/path args (SSRF + request smuggling prevention)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: net-acceptance

# ============================================================================
# Stage 38 (v0.57.0-alpha): std.http -- HTTP/1.1 server + client (RFC 7230)
# ============================================================================
http-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/http_demo.hls > /tmp/http_interp.txt 2>&1
	@bin/hlc examples/http_demo.hls /tmp/http_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/http_demo /tmp/http_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/http_demo > /tmp/http_nat.txt 2>&1
	@diff -q /tmp/http_interp.txt /tmp/http_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: http_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage38_http.hls > /tmp/s38_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage38_http.hls /tmp/s38.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s38 /tmp/s38.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s38 > /tmp/s38_nat.txt 2>&1
	@diff -q /tmp/s38_interp.txt /tmp/s38_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage38_http differential mismatch" && false)
	@rm -f /tmp/http_demo /tmp/http_demo.c /tmp/http_interp.txt /tmp/http_nat.txt \
		/tmp/s38 /tmp/s38.c /tmp/s38_interp.txt /tmp/s38_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 38 -- std.http (HTTP/1.1 server + client, RFC 7230)"
	@echo "  HttpRequest / HttpResponse / HttpHeader structs"
	@echo "  http_parse_request / http_parse_response (RFC 7230 parser)"
	@echo "  http_serialize_request / http_serialize_response (canonical wire format)"
	@echo "  http_get (TCP client) / http_serve (single-connection server)"
	@echo "  http_status_text (canonical reason phrases)"
	@echo "  http_header_get / has / set / add (case-insensitive header list helpers)"
	@echo "  http_text_response / http_html_response / http_json_response (constructors)"
	@echo "  parse-error handling (400 Bad Request on malformed input)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: http-acceptance

