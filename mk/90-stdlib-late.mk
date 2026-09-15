# ============================================================================
# Stage 54 (v0.73.0-alpha): std.tui -- terminal UI primitives (Term,
# Cursor, Color, Style, Rect, Cell, Buffer, Widget trait convention).
# Pure-HLS implementation (no new compiler builtins) -- uses print()
# (IO effect) for emitting ANSI escape codes constructed from chr(27).
# ============================================================================

# Stage 54 tui demo is deterministic (every escape sequence is a pure
# string operation on chr(27); no live input, no random clock). The
# differential test compares interpreter and native output byte-for-byte.
tui-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/tui_demo.hls > /tmp/tui_demo_interp.txt 2>&1
	@bin/hlc examples/tui_demo.hls /tmp/tui_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/tui_demo /tmp/tui_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/tui_demo > /tmp/tui_demo_nat.txt 2>&1
	@diff -q /tmp/tui_demo_interp.txt /tmp/tui_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: tui_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage54_tui.hls > /tmp/s54_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage54_tui.hls /tmp/s54.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s54 /tmp/s54.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s54 > /tmp/s54_nat.txt 2>&1
	@diff -q /tmp/s54_interp.txt /tmp/s54_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage54_tui differential mismatch" && false)
	@rm -f /tmp/tui_demo /tmp/tui_demo.c /tmp/tui_demo_interp.txt /tmp/tui_demo_nat.txt \
		/tmp/s54 /tmp/s54.c /tmp/s54_interp.txt /tmp/s54_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 54 -- std.tui (terminal UI primitives)"
	@echo "  Term (raw / restore / screen / clear / flush) -- ANSI escape sequences"
	@echo "  Cursor (move_to / up / down / left / right / hide / show / save / restore)"
	@echo "  Color (16-colour palette + bright + default + reset; bg conversion)"
	@echo "  Style (fg / bg / bold / dim / italic / underline / blink / reverse / hidden / strikethrough)"
	@echo "  Rect / Cell / Buffer (in-memory grid; pre-allocated; O(W*H) render)"
	@echo "  Widget convention: Text, Block, Paragraph (monomorphic helpers)"
	@echo "  Layout helpers: split_horizontal, split_vertical (weighted constraints)"
	@echo "  Pure-HLS implementation (no new compiler builtins; uses print() IO)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: tui-acceptance

# ============================================================================
# Stage 55 (v0.74.0-alpha): std.color -- terminal color support +
# truecolor/256/16/monochrome fallback + Style builder (cstyle_new()
# .fg(RED).bold()).  Pure-HLS implementation (no new compiler builtins)
# -- uses env_has/env_get (Proc effect, Stage 48) for color detection
# and chr(27) + "[..." (same pattern as std.tui in Stage 54) for ANSI
# escape codes.
# ============================================================================

# Stage 55 color demo + acceptance test are deterministic: every escape
# sequence is a pure string operation on chr(27); the only env reads
# are in color_detect() which is NOT called from the differential paths
# (the test uses color_detect_from(...) with explicit env values, and
# the demo's color_detect() call is wrapped in a println that doesn't
# affect the differential comparison because the live env is identical
# between interpreter and native for the same process).
color-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/color_demo.hls > /tmp/color_demo_interp.txt 2>&1
	@bin/hlc examples/color_demo.hls /tmp/color_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/color_demo /tmp/color_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/color_demo > /tmp/color_demo_nat.txt 2>&1
	@diff -q /tmp/color_demo_interp.txt /tmp/color_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: color_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage55_color.hls > /tmp/s55_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage55_color.hls /tmp/s55.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s55 /tmp/s55.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s55 > /tmp/s55_nat.txt 2>&1
	@diff -q /tmp/s55_interp.txt /tmp/s55_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage55_color differential mismatch" && false)
	@rm -f /tmp/color_demo /tmp/color_demo.c /tmp/color_demo_interp.txt /tmp/color_demo_nat.txt \
		/tmp/s55 /tmp/s55.c /tmp/s55_interp.txt /tmp/s55_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 55 -- std.color (terminal color support + Style builder)"
	@echo "  ColorLevel (None / Basic / 256 / Truecolor) + int encode/decode + ordering + names"
	@echo "  Rgb (0..255 per channel; range validation; equality)"
	@echo "  Color (ColorNone / ColorBasic(int) / Color256(int) / ColorRgb(Rgb))"
	@echo "  Color constructors (named basic / bright / default / reset / rgb / 256 / none)"
	@echo "  color_bg (fg -> bg conversion: 30..37 -> 40..47, 90..97 -> 100..107)"
	@echo "  ColorStyle builder: cstyle_new().fg(RED).bg(BLACK).bold().underline() (method chaining)"
	@echo "  ColorStyle builder: cstyle_fg/cstyle_bg/cstyle_bold/... (free-function form, parity)"
	@echo "  cstyle_to_sgr at every level (truecolor / 256 / basic / none) with down-sampling"
	@echo "  RGB down-sampling: rgb_to_xterm256 (cube + gray), rgb_to_basic_sgr, xterm256_to_basic_sgr"
	@echo "  color_downsample (manual: Rgb -> 256 -> basic -> none, monotone)"
	@echo "  color_render / color_print / color_println (convenience wrappers)"
	@echo "  color_reset_sgr (\\x1b[0m)"
	@echo "  Detection: color_detect() reads NO_COLOR / FORCE_COLOR / CLICOLOR_FORCE / COLORTERM / TERM"
	@echo "  Detection: color_detect_from(...) pure form (differential-safe; explicit env values)"
	@echo "  Bridge: cstyle_to_basic_style(cs) -> std.tui Style (down-samples RGB/256 to basic)"
	@echo "  Pure-HLS implementation (no new compiler builtins; uses env_has/env_get Proc + print IO)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: color-acceptance

# ============================================================================
# Stage 56 (v0.75.0-alpha): std.progress -- progress bars, spinners,
# ETA estimation, multi-bar rendering. THREE new compiler builtins
# (eprint / eprintln: stderr writes with the flush-first discipline;
# isatty(fd): TTY detection -- the probe std.color deferred to this
# stage). Pure-HLS module: the render core is pure (explicit ColorLevel
# + now_ns), the live shell draws to stderr and stays silent when
# stderr is not a TTY (isatty(2)) so piped output is never polluted.
# ============================================================================

# Stage 56 progress demo + acceptance test are deterministic: every
# stdout line is a pure function of fixed inputs (synthetic now_ns /
# ColorLevel), the live-draw section checks isatty(2) first and writes
# NOTHING under redirection (the acceptance harness always redirects),
# and the only forced stderr content is the fixed eprint/eprintln echo
# (both backends flush stdout before the stderr write, so the combined
# capture interleaves byte-identically). The differential tests compare
# interpreter vs native output byte-for-byte.
progress-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/progress_demo.hls > /tmp/progress_demo_interp.txt 2>&1
	@bin/hlc examples/progress_demo.hls /tmp/progress_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/progress_demo /tmp/progress_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/progress_demo > /tmp/progress_demo_nat.txt 2>&1
	@diff -q /tmp/progress_demo_interp.txt /tmp/progress_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: progress_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage56_progress.hls > /tmp/s56_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage56_progress.hls /tmp/s56.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s56 /tmp/s56.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s56 > /tmp/s56_nat.txt 2>&1
	@diff -q /tmp/s56_interp.txt /tmp/s56_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage56_progress differential mismatch" && false)
	@rm -f /tmp/progress_demo /tmp/progress_demo.c /tmp/progress_demo_interp.txt /tmp/progress_demo_nat.txt \
		/tmp/s56 /tmp/s56.c /tmp/s56_interp.txt /tmp/s56_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 56 -- std.progress (progress bars, spinners, ETA)"
	@echo "  ProgressBar: progress_new(total) determinate + indeterminate (total = 0)"
	@echo "  State: inc / set_position / set_message / tick (pure, zero-alloc, clamped)"
	@echo "  Builders: with_label/message/width/chars/head/spinner/color/steady (immutable)"
	@echo "  impl ProgressBar: .start/.inc/.tick/.draw/.finish + pure accessors (roadmap API)"
	@echo "  ETA estimation: integer math + int64 overflow guard (elapsed * remaining / pos)"
	@echo "  Rate + percent + compact duration formatting (45s/3m12s/2h05m/4d03h)"
	@echo "  8 spinner styles (dots/line/dots-ascii/arrow/bounce/toggle/triangle/pipe)"
	@echo "  MultiBar: one line per task, cursor-up + EL 2 redraw, value-snapshot model"
	@echo "  Rendering at every ColorLevel (byte-exact SGR; monochrome is escape-free)"
	@echo "  3 new builtins: eprint/eprintln (stderr, taint sink, fflush-first) + isatty(fd)"
	@echo "  Live draws gated by isatty(2) (+ PROGRESS_FORCE / PROGRESS_DISABLE env)"
	@echo "  --color=always/never/auto surface (progress_color_from_str)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: progress-acceptance

# ============================================================================
# Stage 57 (v0.76.0-alpha): std.log -- structured logging (human + JSON
# + syslog formats, HLS_LOG level control). TWO new compiler builtins
# (proc_pid: the syslog TAG[PID] field; sys_hostname: the HOSTNAME
# field -- both Proc). Pure-HLS module on top of Stage 56's eprint /
# isatty, Stage 55's std.color, Stage 48's env access, and Stage 49's
# wall clock. Logs go to stderr (the 12-factor / systemd convention).
# ============================================================================

# Stage 57 differential note (DIFFERENT from the previous stages): the
# LIVE emit lines carry the wall clock + the real pid, which differ
# between the interpreter process and the native binary process BY
# DESIGN. The differential comparison therefore covers STDOUT ONLY
# (every stdout line is a pure function of fixed ts_ms / pid /
# hostname / ColorLevel); the live stderr path is SMOKE-TESTED (the
# program must exit 0 -- a crash, a panic, or a compile error fails
# the acceptance). This is the same discipline Stage 49 applied to
# the live wall clock.
log-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/log_demo.hls > /tmp/log_demo_interp.txt 2>/dev/null
	@bin/hlc examples/log_demo.hls /tmp/log_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/log_demo /tmp/log_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/log_demo > /tmp/log_demo_nat.txt 2>/dev/null
	@diff -q /tmp/log_demo_interp.txt /tmp/log_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: log_demo differential mismatch (stdout)" && false)
	@/tmp/log_demo >/dev/null 2>&1 || (echo "FAIL: log_demo live smoke" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage57_log.hls > /tmp/s57_interp.txt 2>/dev/null
	@bin/hlc tests/ok/feat_stage57_log.hls /tmp/s57.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s57 /tmp/s57.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s57 > /tmp/s57_nat.txt 2>/dev/null
	@diff -q /tmp/s57_interp.txt /tmp/s57_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage57_log differential mismatch (stdout)" && false)
	@/tmp/s57 >/dev/null 2>&1 || (echo "FAIL: feat_stage57_log live smoke" && false)
	@rm -f /tmp/log_demo /tmp/log_demo.c /tmp/log_demo_interp.txt /tmp/log_demo_nat.txt \
		/tmp/s57 /tmp/s57.c /tmp/s57_interp.txt /tmp/s57_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 57 -- std.log (structured logging: human + JSON + syslog)"
	@echo "  LogLevel ladder (Off < Error < Warn < Info < Debug < Trace) + log_enabled 6x5 matrix"
	@echo "  info!/warn!/error!/debug!/trace! as functions + impl Logger methods (lg.info(...))"
	@echo "  Structured key-value pairs (parallel lists) in all three formats"
	@echo "  HLS_LOG level control (+ the warning alias; lenient info default)"
	@echo "  HLS_LOG_FORMAT format control (human / json / syslog)"
	@echo "  human: ISO-8601 UTC ms + 5-wide level token + colored (byte-exact SGR)"
	@echo "  json: JSON Lines, fixed key order, full escaping (incl. U+2028/U+2029)"
	@echo "  syslog: RFC 3164 (PRI / Mmm-dd / HOSTNAME / TAG[PID]:) via 2 new builtins"
	@echo "  Timestamps: Hinnant civil-from-days, floor-division, leap-day + pre-epoch edges"
	@echo "  Builders (immutable) + method twins + .render / .is_enabled parity"
	@echo "  Live emits to stderr (12-factor convention); suppressed by level"
	@echo "  differential (stdout, interpreter == native) + live smoke (exit 0) verified green."

.PHONY: log-acceptance
# ============================================================================
# Stage 62 (v0.77.0-alpha): std.man -- man-page generator (nroff/groff)
# from a CliParser definition. Pure-HLS module on top of std.cli,
# std.str, std.option, std.result. The render core (man_render) is a
# pure function (no IO); the live entry points (man_write / man_install
# / man_render_default) carry Fs / Clock respectively.
# ============================================================================

# Stage 62 differential note: the man_render core is pure (no IO), so
# the interpreter and the native binary produce byte-identical nroff
# source. The man_render_default path reads the wall clock (Clock
# effect) for today's date -- its output is compared STDOUT-ONLY (the
# date varies by day, but the structural fields are deterministic).
# The man_write / man_install helpers write a file to /tmp; the test
# reads it back and verifies the content matches the rendered string
# (this part is smoke-tested via exit 0; the file content is verified
# by the test's stdout comparison).

man-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/man_demo.hls > /tmp/man_demo_interp.txt 2>&1
	@bin/hlc examples/man_demo.hls /tmp/man_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/man_demo /tmp/man_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/man_demo > /tmp/man_demo_nat.txt 2>&1
	@diff -q /tmp/man_demo_interp.txt /tmp/man_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: man_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage62_man.hls > /tmp/s62_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage62_man.hls /tmp/s62.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s62 /tmp/s62.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s62 > /tmp/s62_nat.txt 2>&1
	@diff -q /tmp/s62_interp.txt /tmp/s62_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage62_man differential mismatch" && false)
	@/tmp/s62 >/dev/null 2>&1 || (echo "FAIL: feat_stage62_man live smoke (exit non-zero)" && false)
	@rm -f /tmp/man_demo /tmp/man_demo.c /tmp/man_demo_interp.txt /tmp/man_demo_nat.txt \
		/tmp/s62 /tmp/s62.c /tmp/s62_interp.txt /tmp/s62_nat.txt /tmp/feat_stage62_man_test.1
	@echo ""
	@echo "ACCEPTANCE OK: Stage 62 -- std.man (man-page generator)"
	@echo "  ManPage builder (man_new + 12 with_* + 3 add_* + impl twins)"
	@echo "  man_render(page, parser): pure nroff source (no IO)"
	@echo "  man_render_default(parser): live clock entry point"
	@echo "  .TH line with NAME SECTION DATE SOURCE MANUAL"
	@echo "  .SH NAME / SYNOPSIS / DESCRIPTION / OPTIONS / POSITIONAL ARGUMENTS"
	@echo "  .SH SUBCOMMANDS / ENVIRONMENT / EXIT STATUS / EXAMPLES"
	@echo "  .SH AUTHORS / BUGS / SEE ALSO / VERSION"
	@echo "  man_escape: groff metacharacter escaping (defense-in-depth)"
	@echo "  man_install_path: canonical install path computation"
	@echo "  man_is_safe_name: rejects /, .., leading -, spaces, special chars"
	@echo "  man_format_iso_date: Hinnant civil-from-days (pre-epoch safe)"
	@echo "  man_split_paragraphs: multi-paragraph DESCRIPTION (.PP separator)"
	@echo "  man_write / man_install: Fs-effect helpers (write_file builtin)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) + live smoke (exit 0) verified green."

.PHONY: man-acceptance

# ============================================================================
# Stage 63 (v0.78.0-alpha): std.http_router -- HTTP router (path
# params, query, middleware, sub-routers). Pure-HLS module on top of
# std.str, std.option, std.result, std.url, std.collections. The
# dispatcher (router_match) is a pure function (no IO); the handler
# invocation is the user's responsibility (the router returns a
# RouterMatch with the handler_id, and the user's main() dispatches
# on the ID).
# ============================================================================

# Stage 63 differential note: router_match is a pure function of
# (Router, method, path) -- all immutable values. The interpreter and
# the native binary produce byte-identical RouterMatch values (the
# test serialises the match to stdout and compares byte-for-byte).
# The demo's dispatch_handler bridges to std.http (HttpResponse), but
# the dispatcher itself is pure (no IO, no Net).

http-router-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/http_router_demo.hls > /tmp/http_router_demo_interp.txt 2>&1
	@bin/hlc examples/http_router_demo.hls /tmp/http_router_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/http_router_demo /tmp/http_router_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/http_router_demo > /tmp/http_router_demo_nat.txt 2>&1
	@diff -q /tmp/http_router_demo_interp.txt /tmp/http_router_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: http_router_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage63_http_router.hls > /tmp/s63_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage63_http_router.hls /tmp/s63.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s63 /tmp/s63.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s63 > /tmp/s63_nat.txt 2>&1
	@diff -q /tmp/s63_interp.txt /tmp/s63_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage63_http_router differential mismatch" && false)
	@rm -f /tmp/http_router_demo /tmp/http_router_demo.c /tmp/http_router_demo_interp.txt /tmp/http_router_demo_nat.txt \
		/tmp/s63 /tmp/s63.c /tmp/s63_interp.txt /tmp/s63_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 63 -- std.http_router (HTTP routing)"
	@echo "  Router builder (router_new + 8 method-specific + 2 use_* + mount)"
	@echo "  impl Router twins (.get/.post/.put/.delete/.patch/.head/.options/.any/.use_mw/.use_for/.mount/.dispatch/.url_for)"
	@echo "  Path parameters (/users/:id captured into params map, url-decoded)"
	@echo "  Wildcard segments (/files/*path captures the rest, /api/* anonymous)"
	@echo "  Multiple params in one pattern (/users/:id/posts/:pid)"
	@echo "  Query parameters (parsed from ?a=1&b=2 via std.url)"
	@echo "  Method-specific + ANY (catch-all method)"
	@echo "  Middleware chains (global + prefix-scoped; onion-model merging)"
	@echo "  Sub-routers (mount under a prefix; nested arbitrarily deep)"
	@echo "  Trailing-slash normalisation (/users/ == /users; / unchanged)"
	@echo "  Method case-insensitivity ('get' -> 'GET')"
	@echo "  URL-decoding of path params (%20 -> ' ', %2F -> '/')"
	@echo "  router_url_for (reverse routing; sub-router prefix prepended)"
	@echo "  router_path_has_prefix (exact-prefix: /api != /api-v2)"
	@echo "  router_compile_pattern / router_split_path / router_list_routes"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: http-router-acceptance
# ============================================================================
# Stage 58 (v0.79.0-alpha): std.config -- layered config loader
# (defaults < file < env < CLI). Pure-HLS TOML parser + JSON config +
# env-var overlay + type coercion + dotted-path lookup. No new builtins.
# ============================================================================

config-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/config_demo.hls > /tmp/config_demo_interp.txt 2>&1
	@bin/hlc examples/config_demo.hls /tmp/config_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/config_demo /tmp/config_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/config_demo > /tmp/config_demo_nat.txt 2>&1
	@diff -q /tmp/config_demo_interp.txt /tmp/config_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: config_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage58_config.hls > /tmp/s58_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage58_config.hls /tmp/s58.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s58 /tmp/s58.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s58 > /tmp/s58_nat.txt 2>&1
	@diff -q /tmp/s58_interp.txt /tmp/s58_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage58_config differential mismatch" && false)
	@rm -f /tmp/config_demo /tmp/config_demo.c /tmp/config_demo_interp.txt /tmp/config_demo_nat.txt \
		/tmp/s58 /tmp/s58.c /tmp/s58_interp.txt /tmp/s58_nat.txt /tmp/feat_stage58_test.toml /tmp/config_demo.toml
	@echo ""
	@echo "ACCEPTANCE OK: Stage 58 -- std.config (layered config loader)"
	@echo "  ConfigSchema builder (str/int/float/bool keys + no_default + env override)"
	@echo "  impl ConfigSchema method twins (.str_key/.int_key/.float_key/.bool_key/.env)"
	@echo "  Pure-HLS TOML parser (tables, dotted headers, inline tables, arrays,"
	@echo "    multi-line strings, literal strings, comments, quoted keys,"
	@echo "    hex/oct/bin ints, inf/nan, exponential floats)"
	@echo "  JSON config (delegates to std.json for content starting with '{')"
	@echo "  Layered loading: defaults < file < env (env wins)"
	@echo "  Type coercion (str->int/float/bool, int->float/bool, float->int, bool->int)"
	@echo "  Dotted-path lookup + config_has / config_set_str/int/float/bool (CLI overlay)"
	@echo "  config_serialize (round-trip TOML) + config_describe (schema listing)"
	@echo "  config_load_file (Fs effect) + config_load (Proc for env)"
	@echo "  ConfigLoadError (layer / key / message) for structured error reporting"
	@echo "  Env-var name derivation (PREFIX_SECTION_KEY) + explicit env_name override"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: config-acceptance

# ============================================================================
# Stage 59 (v0.80.0-alpha): std.complete -- shell-completion generator
# (bash/zsh/fish/powershell). Pure-HLS generators + per-shell escaping.
# ============================================================================

complete-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/complete_demo.hls > /tmp/complete_demo_interp.txt 2>&1
	@bin/hlc examples/complete_demo.hls /tmp/complete_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/complete_demo /tmp/complete_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/complete_demo > /tmp/complete_demo_nat.txt 2>&1
	@diff -q /tmp/complete_demo_interp.txt /tmp/complete_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: complete_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage59_complete.hls > /tmp/s59_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage59_complete.hls /tmp/s59.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s59 /tmp/s59.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s59 > /tmp/s59_nat.txt 2>&1
	@diff -q /tmp/s59_interp.txt /tmp/s59_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage59_complete differential mismatch" && false)
	@rm -f /tmp/complete_demo /tmp/complete_demo.c /tmp/complete_demo_interp.txt /tmp/complete_demo_nat.txt \
		/tmp/s59 /tmp/s59.c /tmp/s59_interp.txt /tmp/s59_nat.txt /tmp/myapp /tmp/_myapp /tmp/myapp.fish
	@echo ""
	@echo "ACCEPTANCE OK: Stage 59 -- std.complete (shell-completion generator)"
	@echo "  Shell kinds: bash / zsh / fish / powershell"
	@echo "  complete_render(parser, name, shell) dispatcher (pure, no IO)"
	@echo "  Bash: _init_completion + compgen -W (long/short opts) + compgen -f (files)"
	@echo "  Zsh: #compdef + _arguments spec + _values for subcommands + value types"
	@echo "  Fish: complete -c name -l/-s/-d/-r lines + subcommand lines"
	@echo "  PowerShell: Register-ArgumentCompleter + scriptblock + Get-ChildItem"
	@echo "  complete_shell_from_str (case-insensitive: bash/sh, zsh, fish, pwsh/ps)"
	@echo "  complete_escape_bash/zsh/fish (single quote -> '\'') + powershell (doubled)"
	@echo "  complete_install_filename (per-shell convention) + complete_install_path"
	@echo "  complete_is_safe_name (rejects /, .., leading -, spaces, special chars)"
	@echo "  complete_list_shells (human-readable listing + install paths)"
	@echo "  complete_install (Fs effect -- write_file to canonical path)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: complete-acceptance

# ============================================================================
# Stage 60 (v0.81.0-alpha): std.hlscli -- cargo-style launcher
# (new/init/run/build/test/bench/doc/publish/clean/update/completion).
# ============================================================================

hlscli-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/hlscli_demo.hls > /tmp/hlscli_demo_interp.txt 2>&1
	@bin/hlc examples/hlscli_demo.hls /tmp/hlscli_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/hlscli_demo /tmp/hlscli_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/hlscli_demo > /tmp/hlscli_demo_nat.txt 2>&1
	@diff -q /tmp/hlscli_demo_interp.txt /tmp/hlscli_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: hlscli_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage60_hlscli.hls > /tmp/s60_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage60_hlscli.hls /tmp/s60.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s60 /tmp/s60.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s60 > /tmp/s60_nat.txt 2>&1
	@diff -q /tmp/s60_interp.txt /tmp/s60_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage60_hlscli differential mismatch" && false)
	@rm -f /tmp/hlscli_demo /tmp/hlscli_demo.c /tmp/hlscli_demo_interp.txt /tmp/hlscli_demo_nat.txt \
		/tmp/s60 /tmp/s60.c /tmp/s60_interp.txt /tmp/s60_nat.txt
	@rm -rf /tmp/test_load_proj
	@echo ""
	@echo "ACCEPTANCE OK: Stage 60 -- std.hlscli (cargo-style launcher)"
	@echo "  hlscli_build_parser (11 subcommands + 6 global options)"
	@echo "  hlscli_main dispatcher (reads argv, parses, dispatches)"
	@echo "  Subcommands: new / init / run / build / test / bench / doc /"
	@echo "    publish / clean / update / completion"
	@echo "  Project scaffolding (hls-pkg.toml + src/main.hls + .gitignore + README.md)"
	@echo "  hlscli_find_project_root (walk up looking for hls-pkg.toml)"
	@echo "  hlscli_load_project (parse hls-pkg.toml via std.config's TOML parser)"
	@echo "  hlscli_is_safe_project_name (rejects /, .., leading -, spaces, dots)"
	@echo "  hlscli_quote_arg / hlscli_is_shell_safe (shell quoting)"
	@echo "  hlscli_template_* (pkg_toml / main_hls / gitignore / readme / doc_html)"
	@echo "  Env-var config (HLS_COMPILER / HLS_TARGET_DIR / HLS_RELEASE)"
	@echo "  Profile selection (debug / release via --release or HLS_RELEASE=1)"
	@echo "  Subprocess spawning via proc_exec (NOT a shell wrapper -- direct exec)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: hlscli-acceptance

# ============================================================================
# Stage 61 (v0.82.0-alpha): std.hlsdoc -- rustdoc-style API docs generator
# (reads # doc comments, produces HTML with cross-refs + search index).
# ============================================================================

hlsdoc-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/hlsdoc_demo.hls > /tmp/hlsdoc_demo_interp.txt 2>&1
	@bin/hlc examples/hlsdoc_demo.hls /tmp/hlsdoc_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/hlsdoc_demo /tmp/hlsdoc_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/hlsdoc_demo > /tmp/hlsdoc_demo_nat.txt 2>&1
	@diff -q /tmp/hlsdoc_demo_interp.txt /tmp/hlsdoc_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: hlsdoc_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage61_hlsdoc.hls > /tmp/s61_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage61_hlsdoc.hls /tmp/s61.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s61 /tmp/s61.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s61 > /tmp/s61_nat.txt 2>&1
	@diff -q /tmp/s61_interp.txt /tmp/s61_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage61_hlsdoc differential mismatch" && false)
	@rm -f /tmp/hlsdoc_demo /tmp/hlsdoc_demo.c /tmp/hlsdoc_demo_interp.txt /tmp/hlsdoc_demo_nat.txt \
		/tmp/s61 /tmp/s61.c /tmp/s61_interp.txt /tmp/s61_nat.txt \
		/tmp/hlsdoc_demo.html /tmp/hlsdoc_demo_search.json \
		/tmp/feat_stage61_test.html /tmp/feat_stage61_search.json
	@echo ""
	@echo "ACCEPTANCE OK: Stage 61 -- std.hlsdoc (rustdoc-style API docs generator)"
	@echo "  hlsdoc_parse_module (extracts fn/struct/enum/impl + doc comments)"
	@echo "  Doc comment convention: # lines above a declaration"
	@echo "  hlsdoc_render_html (complete HTML page with <style> + <script>)"
	@echo "  Cross-references: [name] -> <a href=#name> if name is a declared item"
	@echo "  Section headers: ## Title -> <h3>Title</h3>"
	@echo "  Search index (JSON) + embedded vanilla JS search (no framework)"
	@echo "  hlsdoc_escape_html (security: ampersand lt gt quot apos escaped, script injection blocked)"
	@echo "  hlsdoc_escape_json (JSON string escaping incl. control bytes)"
	@echo "  hlsdoc_split_paragraphs + hlsdoc_module_has_item + hlsdoc_extract_name"
	@echo "  hlsdoc_render_summary (plain-text listing)"
	@echo "  hlsdoc_write_html / hlsdoc_write_search_index (Fs effect)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: hlsdoc-acceptance

# ============================================================================
# Stage 64 (v0.83.0-alpha): std.http_server -- multi-threaded HTTP
# server (keep-alive, work-stealing thread pool, graceful shutdown,
# HTTP/2 preface detection, TLS-termination configuration hooks).
# Pure-HLS module on top of std.http, std.http_router, std.http2,
# std.net, std.sync, std.thread. The pure pieces (config builder,
# HTTP/2 preface detection, keep-alive decision, dispatch wrapper,
# shutdown flag mechanics, inflight counter) are differentially
# verified. The network pieces (accept loop, worker pool, real HTTP/2
# handshake) are exercised in the demo and the manual test, but are
# NOT part of the differential suite (binding a real port would be
# non-deterministic in CI).
# ============================================================================

# Stage 64 differential note: the pure pieces of std.http_server
# (config, preface detection, keep-alive decision, dispatch wrapper,
# shutdown flag, inflight counter) are pure functions of immutable
# inputs. The interpreter and the native binary produce byte-identical
# output on the acceptance test. The demo (examples/http_server_demo.hls)
# is also differentially verified (it constructs configs, dispatches
# requests via router_match + a user dispatch handler, exercises the
# shutdown flag, and renders the HTTP/2 frame byte layout -- all
# deterministic, no actual port bind).

http-server-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/http_server_demo.hls > /tmp/http_server_demo_interp.txt 2>&1
	@bin/hlc examples/http_server_demo.hls /tmp/http_server_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/http_server_demo /tmp/http_server_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/http_server_demo > /tmp/http_server_demo_nat.txt 2>&1
	@diff -q /tmp/http_server_demo_interp.txt /tmp/http_server_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: http_server_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage64_http_server.hls > /tmp/s64_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage64_http_server.hls /tmp/s64.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s64 /tmp/s64.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s64 > /tmp/s64_nat.txt 2>&1
	@diff -q /tmp/s64_interp.txt /tmp/s64_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage64_http_server differential mismatch" && false)
	@rm -f /tmp/http_server_demo /tmp/http_server_demo.c /tmp/http_server_demo_interp.txt /tmp/http_server_demo_nat.txt \
		/tmp/s64 /tmp/s64.c /tmp/s64_interp.txt /tmp/s64_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 64 -- std.http_server (multi-threaded HTTP server)"
	@echo "  HttpServerConfig builder (12 setters: host/port/workers/backlog/keep_alive_max/"
	@echo "    keep_alive_timeout_ms/read_timeout_ms/max_body/graceful_shutdown_ms/http2/tls/alpn)"
	@echo "  HttpServer { config, router, shutdown_mu+flag, inflight_mu+cv+count }"
	@echo "  http_server_new (constructor; no bind)"
	@echo "  http_server_is_shutting_down / request_shutdown / clear_shutdown (Mutex-guarded flag)"
	@echo "  http_server_inflight_begin/end/get (Mutex+Condvar-protected counter)"
	@echo "  http_server_wait_inflight (drain wait; 50ms polling; 0=drained, 1=timeout)"
	@echo "  http_server_http2_preface (RFC 7540 S3.5 24-byte magic, exact byte verification)"
	@echo "  http_server_is_http2_preface (full match, preface+extra, HTTP/1.1 rejection,"
	@echo "    short buffer rejection, empty rejection, almost-preface rejection)"
	@echo "  http_server_should_keep_alive (HTTP/1.0 default=close, HTTP/1.1 default=keep-alive,"
	@echo "    Connection: close/keep-alive header, case-insensitive, max-keepalive cap)"
	@echo "  http_server_route_request (router_match + default http_server_dispatch)"
	@echo "  http_server_handle_http1_conn (HTTP/1.1 keep-alive loop, multi-request)"
	@echo "  http_server_handle_http2_conn (HTTP/2 SETTINGS + ACK + DATA + GOAWAY frame sequence)"
	@echo "  http_server_handle_conn (preface detection dispatch; HTTP/2 vs HTTP/1.1)"
	@echo "  http_server_worker_loop (worker pool; bounded Chan[TcpStream] work queue;"
	@echo "    poison-pill fd<=0 sentinel for clean worker exit)"
	@echo "  http_server_serve (bind + accept loop + spawn N workers + graceful shutdown)"
	@echo "  http_server_work_queue_capacity (2*workers backpressure formula)"
	@echo "  http_server_describe_config (human-readable rendering for --print-config)"
	@echo "  HTTP/2 frame encoders verified byte-level (SETTINGS, SETTINGS_ACK, GOAWAY, DATA on stream 1)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: http-server-acceptance

# ============================================================================
# Stage 65 (v0.84.0-alpha): std.websocket -- RFC 6455 WebSocket protocol
# (server + client handshake, frame encode/decode, masking, close
# handshake). Pure-HLS module on top of std.http, std.http2, std.bits,
# std.sha1 (NEW this stage), std.base64, std.str, std.option,
# std.result, std.net. Also adds std/sha1.hls (RFC 3174 / FIPS 180-4
# SHA-1) -- required by RFC 6455 §1.3 for the Sec-WebSocket-Accept
# derivation. Verified against the FIPS 180-4 test vectors and the
# canonical RFC 6455 §1.3 example (client key dGhlIHNhbXBsZSBub25jZQ==
# -> accept s3pPLMBiTxaQ9kYGzzhZRbK+xOo=). The pure pieces (handshake
# key computation, frame encode/decode, masking, close-code parsing)
# are differentially verified. The network pieces (live send/recv
# helpers) are exercised in the demo but are NOT part of the
# differential suite (binding a real port would be non-deterministic
# in CI).
# ============================================================================

# Stage 65 differential note: the pure pieces of std.websocket
# (accept-key computation, frame encode/decode, masking, close-code
# parsing, HTTP handshake request/response rendering) are pure
# functions of immutable inputs. The interpreter and the native binary
# produce byte-identical output on the acceptance test. The demo
# (examples/websocket_demo.hls) is also differentially verified (it
# computes SHA-1 FIPS vectors, the RFC 6455 §1.3 accept-key example,
# the full 101 response, upgrade validation, accept verification,
# byte-level frame encoding with a fixed mask key, close-frame round
# trip, full client-side handshake request, opcode/close-code names --
# all deterministic, no actual network).

websocket-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/websocket_demo.hls > /tmp/ws_demo_interp.txt 2>&1
	@bin/hlc examples/websocket_demo.hls /tmp/ws_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/ws_demo /tmp/ws_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/ws_demo > /tmp/ws_demo_nat.txt 2>&1
	@diff -q /tmp/ws_demo_interp.txt /tmp/ws_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: websocket_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage65_websocket.hls > /tmp/s65_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage65_websocket.hls /tmp/s65.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s65 /tmp/s65.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s65 > /tmp/s65_nat.txt 2>&1
	@diff -q /tmp/s65_interp.txt /tmp/s65_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage65_websocket differential mismatch" && false)
	@rm -f /tmp/ws_demo /tmp/ws_demo.c /tmp/ws_demo_interp.txt /tmp/ws_demo_nat.txt \
		/tmp/s65 /tmp/s65.c /tmp/s65_interp.txt /tmp/s65_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 65 -- std.websocket (RFC 6455 WebSocket protocol)"
	@echo "  std.sha1 (RFC 3174 / FIPS 180-4): sha1_hash / sha1_hex"
	@echo "    verified against FIPS 180-4 test vectors (empty, 'abc', 448-bit, fox)"
	@echo "  Server-side handshake:"
	@echo "    ws_accept_key (RFC 6455 S1.3: base64(SHA-1(key + GUID)))"
	@echo "    ws_handshake_response (101 Switching Protocols + subprotocol)"
	@echo "    ws_validate_upgrade (RFC 6455 S4.1: method/version/headers validation)"
	@echo "    ws_pick_subprotocol (first of comma-separated list)"
	@echo "  Client-side handshake:"
	@echo "    ws_generate_key (random 16-byte base64)"
	@echo "    ws_handshake_request (full GET /path HTTP/1.1 Upgrade)"
	@echo "    ws_validate_accept (MITM detection)"
	@echo "    ws_validate_upgrade_response (verify 101 + Accept + subprotocol)"
	@echo "  Frame encode/decode (RFC 6455 S5):"
	@echo "    WsFrame { fin, opcode, masked, mask_key, payload }"
	@echo "    ws_frame_encode_with_key (DETERMINISTIC for tests)"
	@echo "    ws_frame_encode (random mask key when mask=true)"
	@echo "    ws_frame_decode (RSV/opcode/control-frame validation;"
	@echo "      Err('incomplete') for partial frames)"
	@echo "    ws_frame_decode_length (total byte length; 0=partial; -1=oversized)"
	@echo "    7-bit / 16-bit / 64-bit payload-length fields (S5.2)"
	@echo "  Masking (RFC 6455 S5.3):"
	@echo "    ws_apply_mask (XOR with mask_key[i mod 4]; involutive)"
	@echo "    ws_generate_mask_key (random 4-byte mask key)"
	@echo "  Close handshake (RFC 6455 S7):"
	@echo "    ws_close_payload / ws_parse_close_payload (WsClosePayload struct)"
	@echo "    ws_is_valid_close_code (S7.4.2: 1000-1003, 1007-1011, 3000-3999,"
	@echo "      4000-4999 valid; 1004-1006, 1012-1015 reserved)"
	@echo "  Connection helpers:"
	@echo "    server-side: ws_send_text/binary/close/ping/pong (UNMASKED)"
	@echo "    client-side: ws_send_text_masked/binary_masked/close_masked (MASKED)"
	@echo "    ws_read_frame (bounded read loop; 32 reads max per frame)"
	@echo "  Constants: opcodes, close codes, frame-size bounds, GUID"
	@echo "  Diagnostics: ws_opcode_name, ws_close_code_name, ws_describe_frame"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: websocket-acceptance

# ============================================================================
# Stage 66 (v0.85.0-alpha): std.cookie -- RFC 6265bis HTTP cookies
# (parse Cookie header, serialise Set-Cookie, sign with HMAC-SHA1,
# SameSite/Secure/HttpOnly attributes). Pure-HLS module on top of
# std.http, std.bits, std.sha1 (NEW in Stage 65), std.base64,
# std.str, std.option, std.result. The pure pieces (parse, serialise,
# sign, verify, format-date, sanitize) are differentially verified.
# The signing layer uses HMAC-SHA1 (RFC 2104) which the module
# implements in pure HLS via std.bits + std.sha1. HMAC-SHA1 remains
# secure per RFC 6221 §6 (SHA-1's collision weakness does NOT
# compromise HMAC).
# ============================================================================

# Stage 66 differential note: the pure pieces of std.cookie (parse,
# serialise, sign, verify, format-http-date, sanitize) are pure
# functions of immutable inputs. The interpreter and the native binary
# produce byte-identical output on the acceptance test. The demo
# (examples/cookie_demo.hls) is also differentially verified (it
# parses a Cookie header, builds a full SetCookie with every attribute,
# signs + verifies + unwraps, formats HTTP dates on canonical vectors,
# sanitises name/value, and does a full HTTP round-trip -- all
# deterministic, no actual I/O).

cookie-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/cookie_demo.hls > /tmp/cookie_demo_interp.txt 2>&1
	@bin/hlc examples/cookie_demo.hls /tmp/cookie_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/cookie_demo /tmp/cookie_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/cookie_demo > /tmp/cookie_demo_nat.txt 2>&1
	@diff -q /tmp/cookie_demo_interp.txt /tmp/cookie_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: cookie_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage66_cookie.hls > /tmp/s66_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage66_cookie.hls /tmp/s66.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s66 /tmp/s66.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s66 > /tmp/s66_nat.txt 2>&1
	@diff -q /tmp/s66_interp.txt /tmp/s66_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage66_cookie differential mismatch" && false)
	@rm -f /tmp/cookie_demo /tmp/cookie_demo.c /tmp/cookie_demo_interp.txt /tmp/cookie_demo_nat.txt \
		/tmp/s66 /tmp/s66.c /tmp/s66_interp.txt /tmp/s66_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 66 -- std.cookie (RFC 6265bis HTTP cookies)"
	@echo "  CookieJar parsing:"
	@echo "    cookie_parse_request / cookie_parse_string (liberal: trims whitespace,"
	@echo "      skips empty pairs, splits on FIRST '=' so values may contain '=')"
	@echo "    cookie_get (FIRST match per RFC 6265 S5.4 longer-path precedence)"
	@echo "    cookie_has / cookie_names (insertion order, deduplicated) / cookie_count"
	@echo "  SetCookie builder + immutable setters:"
	@echo "    cookie_new(name, value)"
	@echo "    cookie_with_expires / _max_age / _domain / _path / _secure / _http_only / _same_site"
	@echo "    (each returns a NEW SetCookie -- same idiom as std.cli, std.log,"
	@echo "     std.http_router, std.http_server)"
	@echo "  Serialisation:"
	@echo "    cookie_serialize (canonical RFC 6265 S4.1.1 form;"
	@echo "      attribute order: Expires/Max-Age/Domain/Path/Secure/HttpOnly/SameSite;"
	@echo "      panics if result > 4096 bytes per RFC 6265 S6.1)"
	@echo "    cookie_to_header (HttpHeader wrapper for response building)"
	@echo "  SameSite constants (RFC 6265bis S5.2):"
	@echo "    cookie_samesite_strict / lax / none / unset"
	@echo "    cookie_samesite_is_valid (case-insensitive)"
	@echo "  Sanitisation (RFC 6265 S4.1.1 grammar):"
	@echo "    cookie_sanitize_name (rejects CTLs/;/,/=,\\\\,space/non-ASCII)"
	@echo "    cookie_sanitize_value (allows '=' and space in values)"
	@echo "  Signing (anti-tampering via HMAC-SHA1, RFC 2104):"
	@echo "    cookie_hmac_sha1(key, msg) -> 20-byte digest (pure HLS via std.bits + std.sha1)"
	@echo "    cookie_sign(sc, secret) -> SetCookie with value = '<orig>.<b64url(sig)>'"
	@echo "    cookie_verify(sc, secret) -> bool (CONSTANT TIME comparison)"
	@echo "    cookie_unwrap_signed(sc, secret) -> Result[str, str] (verify + extract)"
	@echo "    cookie_const_time_eq (XOR every byte; OR; no short-circuit)"
	@echo "  HTTP date formatting:"
	@echo "    cookie_format_http_date(unix_ms) -> RFC 7231 S7.1.1.1 IMF-fixdate"
	@echo "      (e.g. 'Sun, 06 Nov 1994 08:49:37 GMT')"
	@echo "      uses Hinnant civil-from-days (same as std.log Stage 57)"
	@echo "  Diagnostics: cookie_describe(sc)"
	@echo "  Security model: SECURE + HTTP-ONLY + SAMESITE + SIGNED are"
	@echo "    COMPLEMENTARY defenses (sniffing / XSS-hijack / CSRF / forgery)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: cookie-acceptance

# ============================================================================
# Stage 67 (v0.86.0-alpha): std.session -- server-side session storage
# (in-memory + file-backed). Pure-HLS module on top of std.cookie
# (Stage 66 signed cookies), std.http, std.bits, std.sha1, std.base64,
# std.str, std.option, std.result, + global Fs builtins (read_file,
# write_file, file_exists, fs_read_dir). The session ID is a 32-byte
# random value, base64url-encoded (43 chars; 256-bit entropy), deliv-
# ered via a signed + Secure + HttpOnly + SameSite=Lax cookie. The
# file-backed store uses one file per session at <dir>/<id>.sess, with
# path-traversal defense (ID validated before use as a filename).
# ============================================================================

# Stage 67 differential note: the pure pieces of std.session (ID
# generation, validation, in-memory store CRUD, serialise/parse round
# trip, cookie issue/parse, session_describe) are pure functions of
# immutable inputs. The interpreter and the native binary produce
# byte-identical output on the acceptance test. The demo exercises
# the file-backed store with cleanup (rand_seed(42) for deterministic
# ID generation; /tmp/hls_session_demo/ cleaned before each run), so
# its output is also deterministic and differential-safe.

session-acceptance: bin/hlc
	@mkdir -p /tmp/hls_session_demo
	@rm -f /tmp/hls_session_demo/*.sess 2>/dev/null || true
	@$(PYTHON) boot/boot.py examples/session_demo.hls > /tmp/session_demo_interp.txt 2>&1
	@bin/hlc examples/session_demo.hls /tmp/session_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/session_demo /tmp/session_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@rm -f /tmp/hls_session_demo/*.sess 2>/dev/null || true
	@/tmp/session_demo > /tmp/session_demo_nat.txt 2>&1
	@diff -q /tmp/session_demo_interp.txt /tmp/session_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: session_demo differential mismatch" && false)
	@mkdir -p /tmp/hls_session_test
	@rm -f /tmp/hls_session_test/*.sess 2>/dev/null || true
	@$(PYTHON) boot/boot.py tests/ok/feat_stage67_session.hls > /tmp/s67_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage67_session.hls /tmp/s67.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s67 /tmp/s67.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@rm -f /tmp/hls_session_test/*.sess 2>/dev/null || true
	@/tmp/s67 > /tmp/s67_nat.txt 2>&1
	@diff -q /tmp/s67_interp.txt /tmp/s67_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage67_session differential mismatch" && false)
	@rm -f /tmp/session_demo /tmp/session_demo.c /tmp/session_demo_interp.txt /tmp/session_demo_nat.txt \
		/tmp/s67 /tmp/s67.c /tmp/s67_interp.txt /tmp/s67_nat.txt
	@rm -f /tmp/hls_session_demo/*.sess /tmp/hls_session_test/*.sess 2>/dev/null || true
	@echo ""
	@echo "ACCEPTANCE OK: Stage 67 -- std.session (server-side sessions)"
	@echo "  Session record:"
	@echo "    session_new / _get / _set / _has / _keys / _is_expired / _touch / _extend"
	@echo "    session_describe (human-readable rendering)"
	@echo "  ID generation & validation:"
	@echo "    session_generate_id (32 bytes -> base64url, 43 chars; 256-bit entropy)"
	@echo "    session_is_valid_id (charset + length check; rejects path traversal)"
	@echo "    session_sanitize_id (defensive; returns '' for invalid input)"
	@echo "  In-memory SessionStore:"
	@echo "    session_store_new / _create / _get / _save / _destroy"
	@echo "    session_store_reap (count expired) / _compact (true removal)"
	@echo "    session_store_count"
	@echo "  File-backed SessionFileStore:"
	@echo "    session_file_store_new / _create / _get / _save / _destroy"
	@echo "    session_file_store_reap / _count / _path (path-traversal defense)"
	@echo "  Serialisation (text format v1):"
	@echo "    session_serialize (id + timestamps + key=value lines)"
	@echo "    session_parse (Result[Session, str]; rejects bad version/truncated/bad ID)"
	@echo "  Cookie glue (signed + Secure + HttpOnly + SameSite=Lax):"
	@echo "    session_issue_cookie (HMAC-SHA1 signed via std.cookie Stage 66)"
	@echo "    session_attach_cookie (append Set-Cookie to response)"
	@echo "    session_destroy_cookie (Max-Age=0 logout)"
	@echo "    session_id_from_request (parse + verify + extract)"
	@echo "    session_load_from_request / session_load_from_request_file"
	@echo "  Security model: SECURE + HTTP-ONLY + SAMESITE=LAX + SIGNED are"
	@echo "    COMPLEMENTARY defenses (sniffing / XSS-hijack / CSRF / forgery)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: session-acceptance

# ============================================================================
# Stage 68 (v0.87.0-alpha): std.csrf -- CSRF protection (double-submit
# cookie + synchronizer-token patterns). Pure-HLS module on top of
# std.cookie (Stage 66 signed cookies, cookie_const_time_eq), std.http,
# std.base64, std.str, std.option, std.result. The CSRF token is a
# 32-byte random value, base64url-encoded (43 chars; 256-bit entropy).
# Double-submit: NON-HttpOnly cookie + X-CSRF-Token header (constant-
# time compare). Synchronizer: per-session server-side token store
# (constant-time compare). Includes safe-method classification
# (GET/HEAD/OPTIONS safe; POST/PUT/PATCH/DELETE require token).
# ============================================================================

# Stage 68 differential note: all pieces of std.csrf are pure functions
# of immutable inputs (token generation is seeded via rand_seed(42) for
# determinism). The interpreter and the native binary produce byte-
# identical output on both the demo and the acceptance test.

csrf-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/csrf_demo.hls > /tmp/csrf_demo_interp.txt 2>&1
	@bin/hlc examples/csrf_demo.hls /tmp/csrf_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/csrf_demo /tmp/csrf_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/csrf_demo > /tmp/csrf_demo_nat.txt 2>&1
	@diff -q /tmp/csrf_demo_interp.txt /tmp/csrf_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: csrf_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage68_csrf.hls > /tmp/s68_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage68_csrf.hls /tmp/s68.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s68 /tmp/s68.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s68 > /tmp/s68_nat.txt 2>&1
	@diff -q /tmp/s68_interp.txt /tmp/s68_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage68_csrf differential mismatch" && false)
	@rm -f /tmp/csrf_demo /tmp/csrf_demo.c /tmp/csrf_demo_interp.txt /tmp/csrf_demo_nat.txt \
		/tmp/s68 /tmp/s68.c /tmp/s68_interp.txt /tmp/s68_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 68 -- std.csrf (CSRF protection)"
	@echo "  Token generation & validation:"
	@echo "    csrf_generate_token (32 bytes -> base64url, 43 chars; 256-bit entropy)"
	@echo "    csrf_is_valid_token (charset + length check)"
	@echo "    csrf_const_time_eq (wraps cookie_const_time_eq Stage 66)"
	@echo "  Double-submit cookie pattern (stateless):"
	@echo "    csrf_double_submit_cookie (NON-HttpOnly, Secure, SameSite=Lax)"
	@echo "    csrf_double_submit_extract (X-CSRF-Token header)"
	@echo "    csrf_double_submit_cookie_value (csrf cookie value)"
	@echo "    csrf_double_submit_validate (header == cookie == expected, const-time)"
	@echo "    csrf_double_submit_issue (append Set-Cookie to response)"
	@echo "  Synchronizer-token pattern (stateful, per-session):"
	@echo "    CsrfStore + csrf_store_new / _issue / _validate / _invalidate"
	@echo "    csrf_store_reap (count expired) / _compact (true removal) / _count"
	@echo "  HTTP integration:"
	@echo "    csrf_extract_token (header first, then form field)"
	@echo "    csrf_validate_request (convenience wrapper)"
	@echo "    csrf_is_safe_method (GET/HEAD/OPTIONS) / csrf_require_token (POST/PUT/PATCH/DELETE)"
	@echo "    csrf_describe_failure (human-readable error)"
	@echo "    csrf_check_request (high-level: safe-method + token check)"
	@echo "  Security model: SAMESITE=LAX + DOUBLE-SUBMIT + SYNC-TOKEN are"
	@echo "    COMPLEMENTARY defenses (CSRF / cross-site forgery)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: csrf-acceptance

# ============================================================================
# Stage 69 (v0.88.0-alpha): std.template -- compile-time HTML templates
# (XSS-safe by construction). Pure-HLS module on top of std.html
# (html_escape — Stage 38), std.str, std.option, std.result, std.list.
# A Mustache-like template engine: {{var}} (HTML-escaped by default),
# {{{var}}} (raw, explicit opt-in for trusted HTML), {{#if}}/{{else}}/{{/if}},
# {{#each}}/{{this}}/{{@index}}/{{else}}/{{/each}}, {{! comment }},
# {{> partial}} (registered partials), {{name | filter}} (upper/lower/trim/
# default). The template source is parsed ONCE into a Template AST (a
# tree of TemplateNode values); subsequent renders walk the AST without
# re-parsing. NO code generation; the renderer is a tree-walking
# interpreter. SSTI is impossible by construction (no eval; only variable
# lookups).
# ============================================================================

# Stage 69 differential note: all pieces of std.template are pure functions
# of immutable inputs. The interpreter and the native binary produce byte-
# identical output on both the demo and the acceptance test.

template-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/template_demo.hls > /tmp/tmpl_demo_interp.txt 2>&1
	@bin/hlc examples/template_demo.hls /tmp/tmpl_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/tmpl_demo /tmp/tmpl_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/tmpl_demo > /tmp/tmpl_demo_nat.txt 2>&1
	@diff -q /tmp/tmpl_demo_interp.txt /tmp/tmpl_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: template_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage69_template.hls > /tmp/s69_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage69_template.hls /tmp/s69.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s69 /tmp/s69.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s69 > /tmp/s69_nat.txt 2>&1
	@diff -q /tmp/s69_interp.txt /tmp/s69_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage69_template differential mismatch" && false)
	@rm -f /tmp/tmpl_demo /tmp/tmpl_demo.c /tmp/tmpl_demo_interp.txt /tmp/tmpl_demo_nat.txt \
		/tmp/s69 /tmp/s69.c /tmp/s69_interp.txt /tmp/s69_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 69 -- std.template (compile-time HTML templates)"
	@echo "  Parser (template_compile / template_validate):"
	@echo "    {{var}}       -- variable, HTML-escaped by default"
	@echo "    {{{var}}}     -- raw interpolation, NOT escaped (opt-in)"
	@echo "    {{#if c}}A{{else}}B{{/if}} -- conditional (truthy: non-empty non-false non-0)"
	@echo "    {{#each l}}A{{this}}{{@index}}{{else}}B{{/each}} -- iteration"
	@echo "    {{! comment }}   -- omitted from output"
	@echo "    {{> partial}}    -- include registered partial"
	@echo "    {{name | filter}} -- upper / lower / trim / default / unknown"
	@echo "  Renderer:"
	@echo "    template_render (no partials) / template_render_with_partials"
	@echo "    XSS-safe by construction (every {{var}} escaped via std.html.html_escape)"
	@echo "    SSTI impossible (no eval; only variable lookups)"
	@echo "  AST introspection:"
	@echo "    template_describe (per-node summary, indented for blocks)"
	@echo "    template_node_count / template_top_level_node_count"
	@echo "  Partials registry:"
	@echo "    Partials + partials_new / _register / _register_compiled"
	@echo "    partials_has / _get / _count"
	@echo "  TemplateContext:"
	@echo "    template_context_new / _set / _set_list / _get / _get_list"
	@echo "  Security model: DEFAULT-ESCAPE + RAW-OPT-IN + NO-EVAL are"
	@echo "    COMPLEMENTARY defenses (XSS / injection / SSTI)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: template-acceptance

# ============================================================================
# Stage 70 (v0.89.0-alpha): std.sse -- Server-Sent Events (one-way
# streaming, HTML5 §9.2). Pure-HLS module on top of std.http (Stage 38),
# std.str, std.option, std.result. Implements the SSE wire format:
# event/data/id/retry fields, multi-line data (each line a separate
# "data:" field), comments (": text"), heartbeats (": ping"). Provides
# the SseEvent builder, sse_serialize (wire format), sse_parse (single
# event) / sse_parse_stream (multiple events, heartbeats filtered),
# sse_response (HTTP 200, Content-Type: text/event-stream, Cache-Control:
# no-cache no-transform, Connection: keep-alive, X-Accel-Buffering: no),
# sse_last_event_id / sse_validate_request_id (Last-Event-ID header for
# resumable streams). Stream helpers: sse_build_message / _status_update
# / _notification / _log_line / _heartbeat_block.
# ============================================================================

# Stage 70 differential note: all pieces of std.sse are pure functions
# of immutable inputs. The interpreter and the native binary produce
# byte-identical output on both the demo and the acceptance test.

sse-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/sse_demo.hls > /tmp/sse_demo_interp.txt 2>&1
	@bin/hlc examples/sse_demo.hls /tmp/sse_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/sse_demo /tmp/sse_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/sse_demo > /tmp/sse_demo_nat.txt 2>&1
	@diff -q /tmp/sse_demo_interp.txt /tmp/sse_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: sse_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage70_sse.hls > /tmp/s70_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage70_sse.hls /tmp/s70.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s70 /tmp/s70.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s70 > /tmp/s70_nat.txt 2>&1
	@diff -q /tmp/s70_interp.txt /tmp/s70_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage70_sse differential mismatch" && false)
	@rm -f /tmp/sse_demo /tmp/sse_demo.c /tmp/sse_demo_interp.txt /tmp/sse_demo_nat.txt \
		/tmp/s70 /tmp/s70.c /tmp/s70_interp.txt /tmp/s70_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 70 -- std.sse (Server-Sent Events)"
	@echo "  Event builder (SseEvent struct):"
	@echo "    sse_event_new / sse_event_new_data"
	@echo "    sse_with_id / _with_event / _with_data / _add_data / _with_retry (clamping)"
	@echo "  Wire format (HTML5 §9.2):"
	@echo "    sse_serialize (id/event/retry/data fields; multi-line data; empty data)"
	@echo "    sse_parse (single event; case-insensitive; CRLF; comments; unknown ignored)"
	@echo "    sse_parse_stream (multiple events; heartbeats filtered; partial trailing dropped)"
	@echo "  Comments & heartbeats:"
	@echo "    sse_comment (': text\n\n'; CR/LF sanitized)"
	@echo "    sse_heartbeat (':ping\n\n'; keep-alive through idle proxies)"
	@echo "  HTTP integration:"
	@echo "    sse_response (200, Content-Type: text/event-stream,"
	@echo "      Cache-Control: no-cache no-transform, Connection: keep-alive,"
	@echo "      X-Accel-Buffering: no)"
	@echo "    sse_response_with_events / sse_response_with_heartbeat"
	@echo "    sse_last_event_id / sse_validate_request_id (Last-Event-ID for resume)"
	@echo "  Validation:"
	@echo "    sse_validate_event (limits + CR/LF rejection)"
	@echo "    sse_validate_id / sse_validate_event_type"
	@echo "  Introspection & stream helpers:"
	@echo "    sse_describe (per-event summary, data truncated to 60 chars)"
	@echo "    sse_build_message / _status_update / _notification / _log_line"
	@echo "    sse_build_heartbeat_block (count heartbeats)"
	@echo "  Security model: NO-ORIGIN-CHECK + NO-AUTH-CHECK + ID-VALIDATION"
	@echo "    + DATA-INJECTION-PREVENTION (multi-line data split on \n)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: sse-acceptance

# graphql-acceptance: Stage 71 gate — the demo AND the acceptance
# test run differentially (interpreter == native), proving the whole
# GraphQL pipeline (SDL parse -> query parse -> validate -> variables
# -> trampoline execute -> render) is deterministic.
graphql-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/graphql_demo.hls > /tmp/gql_demo_interp.txt 2>&1
	@bin/hlc examples/graphql_demo.hls /tmp/gql_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/gql_demo /tmp/gql_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/gql_demo > /tmp/gql_demo_nat.txt 2>&1
	@diff -q /tmp/gql_demo_interp.txt /tmp/gql_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: graphql_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage71_graphql.hls > /tmp/s71_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage71_graphql.hls /tmp/s71.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s71 /tmp/s71.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s71 > /tmp/s71_nat.txt 2>&1
	@diff -q /tmp/s71_interp.txt /tmp/s71_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage71_graphql differential mismatch" && false)
	@rm -f /tmp/gql_demo /tmp/gql_demo.c /tmp/gql_demo_interp.txt /tmp/gql_demo_nat.txt \
		/tmp/s71 /tmp/s71.c /tmp/s71_interp.txt /tmp/s71_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 71 -- std.graphql (schema-first GraphQL server)"
	@echo "  SDL parser (type/enum/scalar/schema blocks, descriptions, directives skipped):"
	@echo "    graphql_parse_schema -> Result[GqlSchema, str]"
	@echo "    validation: unique types/fields/enum values, known type refs, Query root"
	@echo "  Document parser:"
	@echo "    graphql_parse_query: anonymous shorthand, named query/mutation,"
	@echo "      variables + defaults, aliases, argument literals, fragments,"
	@echo "      inline fragments; subscriptions REJECTED (use std.sse)"
	@echo "  Anti-ReDoS bounds (the roadmap's constant-memory promise):"
	@echo "    document 16 KiB / depth 10 / nodes 2048 (post-expansion)"
	@echo "    / list literals 256 / args 16 / fragments 32 / variables 32"
	@echo "  Static validation:"
	@echo "    field existence, leaf-vs-composite selection sets, argument"
	@echo "    names/types/required, variable declaration, fragment usage,"
	@echo "    cycle detection, post-flatten collisions"
	@echo "  Trampoline executor (no fn pointers - std.http_router pattern):"
	@echo "    graphql_begin/has_pending/next_task/complete/fail_task/finish"
	@echo "    GqlResolver table -> handler_id; args_json with defaults applied;"
	@echo "    source_json parent values; __typename meta-field"
	@echo "    null propagation through non-null parents; errors with"
	@echo "    message + path + locations; dynamic node budget -> field error"
	@echo "  HTTP glue: graphql_http_request (POST JSON + GET query string),"
	@echo "    graphql_http_response / graphql_http_error (400)"
	@echo "  Schema export: graphql_schema_sdl (canonical, round-trip fixed"
	@echo "    point), graphql_schema_to_json (structural introspection view)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: graphql-acceptance

# openapi-acceptance: Stage 72 gate — the demo AND the acceptance
# test run differentially (interpreter == native), proving the whole
# OpenAPI pipeline (schema builders -> router bridge -> validate ->
# render -> serve) is deterministic.
openapi-acceptance: bin/hlc
	@$(PYTHON) boot/boot.py examples/openapi_demo.hls > /tmp/oa_demo_interp.txt 2>&1
	@bin/hlc examples/openapi_demo.hls /tmp/oa_demo.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/oa_demo /tmp/oa_demo.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/oa_demo > /tmp/oa_demo_nat.txt 2>&1
	@diff -q /tmp/oa_demo_interp.txt /tmp/oa_demo_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: openapi_demo differential mismatch" && false)
	@$(PYTHON) boot/boot.py tests/ok/feat_stage72_openapi.hls > /tmp/s72_interp.txt 2>&1
	@bin/hlc tests/ok/feat_stage72_openapi.hls /tmp/s72.c 2>/dev/null
	@gcc -O2 $(HL_CURL_DEFS) $(LIBCURL_CFLAGS) -o /tmp/s72 /tmp/s72.c -lm -pthread $(LIBCURL_LIBS) 2>/dev/null
	@/tmp/s72 > /tmp/s72_nat.txt 2>&1
	@diff -q /tmp/s72_interp.txt /tmp/s72_nat.txt >/dev/null 2>&1 \
		|| (echo "FAIL: feat_stage72_openapi differential mismatch" && false)
	@rm -f /tmp/oa_demo /tmp/oa_demo.c /tmp/oa_demo_interp.txt /tmp/oa_demo_nat.txt \
		/tmp/s72 /tmp/s72.c /tmp/s72_interp.txt /tmp/s72_nat.txt
	@echo ""
	@echo "ACCEPTANCE OK: Stage 72 -- std.openapi (OpenAPI 3.1 from handler types)"
	@echo "  JSON Schema builders (3.1 / JSON Schema 2020-12):"
	@echo "    oa_int/int32/float/str/str_format/bool/str_enum/array/object/ref"
	@echo "    oa_nullable (type arrays, NOT 3.0 nullable:true), with_description,"
	@echo "    with_example; oa_schema_to_json (deterministic key order)"
	@echo "  Operation model:"
	@echo "    oa_operation + with_summary/description/tag/param/request_body/"
	@echo "    response/deprecated (free fns + impl method forms); duplicate"
	@echo "    response statuses replace"
	@echo "  Router bridge (the roadmap's promise):"
	@echo "    openapi_from_router: ':param' -> '{param}' path conversion,"
	@echo "    implied path parameters auto-generated (required, string),"
	@echo "    handler_id bindings, ANY routes use the operation's method,"
	@echo "    sub-router mounts contribute prefixed routes, unbound routes"
	@echo "    skipped, unbound bindings rejected"
	@echo "  Document: openapi_doc_new + with_description/server/schema;"
	@echo "    add_operation rejects duplicate path+method / operationId"
	@echo "  Validation: path shape, responses present + described,"
	@echo "    path-template <-> declared parameters, required path params,"
	@echo "    $ref targets resolve (recursively)"
	@echo "  Rendering: openapi_to_json (paths grouped by path then method,"
	@echo "    lower-case method keys, components.schemas, servers, info)"
	@echo "  Serving: openapi_json_response (/openapi.json) and"
	@echo "    openapi_docs_response (/docs Swagger UI, spec embedded INLINE)"
	@echo "    + the URL variant; '</' escaped to '<\/' in embedded specs"
	@echo "    (script-injection defence, JSON still valid)"
	@echo "  Immutability: every builder copies (structs are by-reference)"
	@echo "  Pure-HLS implementation (no new compiler builtins)"
	@echo "  differential (interpreter == native) verified green."

.PHONY: openapi-acceptance

# jsffi-acceptance: Stage 73 gate. extern "js" functions exist ONLY on
# the wasm32 target (the interpreter/native backends reject them), so
# the gate is NOT differential: it compiles the demo with hlwasm and
# runs tools/jsffi_check.js in Node.js (auto struct registration,
# struct round trip, JS->HLS callbacks, main markers).
jsffi-acceptance:
	@echo "[Stage 73 acceptance] compiling examples/jsffi_stage73_demo.hls..."
	@mkdir -p $(BIN)
	@$(PYTHON) tools/hlwasm.py examples/jsffi_stage73_demo.hls $(BIN)/jsffi73 \
	  >$(BIN)/jsffi73.log 2>&1 || (echo "FAIL: compile"; cat $(BIN)/jsffi73.log; exit 1)
	@if ! command -v node >/dev/null 2>&1; then \
	  echo "  node run: SKIP (node not installed)"; \
	  echo "ACCEPTANCE OK: Stage 73 (compile-only — node missing)"; \
	else \
	  node tools/jsffi_check.js $(BIN)/jsffi73.wasm $(BIN)/jsffi73.js \
	    || (echo "FAIL: jsffi acceptance"; exit 1); \
	fi
	@rm -f $(BIN)/jsffi73 $(BIN)/jsffi73.wasm $(BIN)/jsffi73.js $(BIN)/jsffi73.html $(BIN)/jsffi73.log
	@echo "ACCEPTANCE OK: Stage 73 -- std.jsffi (JS FFI for the wasm32 target)"
	@echo "  extern \"js\" surface: 13 -> 48 declarations (console extras,"
	@echo "    DOM attributes/classes/styles/values, JSON bridge, URL encode,"
	@echo "    fetch-with-options, localStorage remove/clear/size, platform/"
	@echo "    language/online/user-agent/screen, performance timing, ISO date)"
	@echo "  AUTO struct marshalling: hlwasm computes field layouts and"
	@echo "    exports hl_struct_descriptors(); the glue registers every"
	@echo "    struct at instantiation (a Halis struct becomes a JS object,"
	@echo "    zero manual registration)"
	@echo "  readStruct str-field fix: the Stage 24 glue read strings AT the"
	@echo "    field address; Stage 73 dereferences the {len,bytes} pointer"
	@echo "  JS->HLS callbacks: define fn jsffi_on_callback(cb_id: int,"
	@echo "    arg: str) -> str; hlwasm exports hl_call_halis; JS reaches it"
	@echo "    via Halis.callHalis(cbId, json) (handler_id dispatch — the"
	@echo "    std.http_router pattern)"
	@echo "  std.jsffi_callback: bookkeeping module (JsffiCallback table) for"
	@echo "    the native/C backend"
	@echo "  webapp-acceptance still green (glue limit 5 KB -> 16 KB; the"
	@echo "    Stage 24 compact-glue milestone stays in the history)"

.PHONY: jsffi-acceptance
