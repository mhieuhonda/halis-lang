"""Interp mixin (builtin) - verbatim segment of the original
boot/interp.py Interp class (lines 1368..2581), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    HLChan, HLPanic, INT64_MIN_SENTINEL, _NAN_DEFAULT, _TARGET_FEATURES, _cpu_supports, _sandbox_check, fmt_float,
    math, os, parse_int, subprocess, sys, threading, time, to_display,
)

class InterpBuiltin(object):
    def builtin(self, name, args, arg_nodes=None):
        line = self.line
        if name == "print":
            self.out.write(args[0])
            return None
        if name == "println":
            self.out.write(args[0] + b"\n")
            return None
        # ----- Stage 56 (v0.75.0-alpha): stderr + TTY-detection builtins -----
        # eprint / eprintln write to stderr. They flush self.out (the
        # stdout buffer) FIRST so interleaved stdout/stderr output keeps
        # the caller's logical order — the interpreter-side mirror of
        # the C runtime's fflush(stdout) in hl_eprint / hl_eprintln
        # (without it, Python's buffered stdout would land AFTER the
        # unbuffered stderr writes in a combined capture, diverging
        # from the native binary's byte order). sys.stderr.buffer is
        # flushed after the write so the bytes leave immediately (the
        # ordering guarantee holds even under 2>&1 redirection).
        if name == "eprint":
            self.out.flush()
            sys.stderr.buffer.write(args[0])
            sys.stderr.buffer.flush()
            return None
        if name == "eprintln":
            self.out.flush()
            sys.stderr.buffer.write(args[0] + b"\n")
            sys.stderr.buffer.flush()
            return None
        # isatty(fd) — true when the file descriptor refers to a
        # terminal. os.isatty mirrors C isatty() semantics: an invalid
        # fd (negative / not open) simply returns False (EBADF), never
        # raises in practice; the defensive except keeps the contract
        # total (C's isatty also returns 0 on EBADF — no panic).
        if name == "isatty":
            try:
                return bool(os.isatty(args[0]))
            except (OSError, ValueError):
                return False
        # ----- Stage 57 (v0.76.0-alpha): process-identity builtins -----
        # proc_pid() — the OS process id (the syslog TAG[PID] field).
        # sys_hostname() — the host name (the syslog HOSTNAME field);
        # os.uname().nodename is the POSIX source (socket.gethostname
        # needs another import; uname is already importable via os).
        # The "localhost" fallback mirrors the C runtime's total
        # behaviour (no panic path).
        if name == "proc_pid":
            return int(os.getpid())
        if name == "sys_hostname":
            try:
                return os.uname().nodename.encode("utf-8", "surrogateescape")
            except (OSError, AttributeError):
                return b"localhost"
        if name == "panic":
            raise HLPanic(args[0], line)
        if name == "exit":
            self.out.flush()
            raise SystemExit(int(args[0]) & 0xFF)
        if name == "str":
            v = args[0]
            if type(v) is bool:
                return b"true" if v else b"false"
            if type(v) is int:
                return str(v).encode("ascii")
            if type(v) is float:
                return fmt_float(v)
            return v
        if name == "int":
            return parse_int(args[0], line)
        if name == "len":
            return len(args[0])
        if name == "range":
            # Deep-scan-7 fix: a malicious or buggy program calling
            # `range(0, INT64_MAX)` would attempt to materialise a
            # 9-quintillion-element list, exhausting memory. Cap at
            # a reasonable limit (1M elements) and panic with a clear
            # message otherwise. boot.py's main thread catches
            # MemoryError, but list(range(...)) raises MemoryError
            # AT THE PYTHON LEVEL — we want a clean HLPanic instead.
            a, b = int(args[0]), int(args[1])
            count = b - a if b > a else 0
            RANGE_MAX = 1_000_000
            if count > RANGE_MAX:
                raise HLPanic(
                    "range(%d, %d) would produce %d elements (limit %d) — "
                    "use an explicit counter loop for large ranges"
                    % (a, b, count, RANGE_MAX), line)
            return list(range(a, b))
        # Stage 21 (v0.37.0-alpha): has_feature — compile-time constant
        # from the --target-feature flag (set via _set_target_feature).
        if name == "has_feature":
            feat = args[0]
            if isinstance(feat, bytes):
                feat = feat.decode("utf-8", "replace")
            return feat in _TARGET_FEATURES
        # Stage 21: simd_cpu_supports — runtime CPU probe (/proc/cpuinfo
        # on Linux; conservative False elsewhere). Matches the C
        # runtime's __builtin_cpu_supports for the probed names.
        if name == "simd_cpu_supports":
            return _cpu_supports(args[0])
        # Stage 19 (v0.35.0-alpha): O(n) join(list[str], sep) -> str.
        # Matches the C runtime's hl_str_join (single allocation, one
        # copy per element). The interpreter's str.join is likewise
        # linear, so differential outputs stay byte-identical.
        if name == "join":
            parts = args[0]
            sep = args[1]
            if not isinstance(parts, list):
                raise HLPanic("join() expects a list[str]", line)
            return sep.join(parts)
        # Stage 32 (v0.51.0-alpha): native bitwise primitives — the
        # interpreter mirrors the C semantics exactly. Shifts are
        # masked to 6 bits (matching the C `& 63`); popcount/clz/ctz
        # use Python's bit_length so they match __builtin_clzll on
        # 64-bit values. All operands are int64 (Python ints; Halis
        # invariant: every int value is in [-2^63, 2^63)).
        #
        # i64_wrap: bitwise ops in Python produce arbitrary-precision
        # ints; we wrap back to signed int64 to match the C semantics
        # (where the result of `(int64_t)(uint64_t)x >> n` is the
        # two's-complement reinterpretation of the unsigned result).
        def _i64_wrap(v):
            v &= (1 << 64) - 1
            if v >= (1 << 63):
                v -= (1 << 64)
            return v
        if name == "int_and":
            return _i64_wrap(args[0] & args[1])
        if name == "int_or":
            return _i64_wrap(args[0] | args[1])
        if name == "int_xor":
            return _i64_wrap(args[0] ^ args[1])
        if name == "int_not":
            return _i64_wrap(~args[0])
        if name == "int_shl":
            n = args[1] & 63
            return _i64_wrap(args[0] << n)
        if name == "int_shr":
            n = args[1] & 63
            # logical right shift on unsigned 64-bit
            x = args[0] & ((1 << 64) - 1)
            return _i64_wrap(x >> n)
        if name == "int_sar":
            n = args[1] & 63
            # arithmetic right shift: Python's >> on negative ints
            # already does sign-extension (floor division by 2^n).
            return _i64_wrap(args[0] >> n)
        if name == "int_popcount":
            x = args[0] & ((1 << 64) - 1)
            return bin(x).count("1")
        if name == "int_clz":
            x = args[0] & ((1 << 64) - 1)
            if x == 0:
                return 64
            return 64 - x.bit_length()
        if name == "int_ctz":
            x = args[0] & ((1 << 64) - 1)
            if x == 0:
                return 64
            n = 0
            while (x & 1) == 0:
                x >>= 1
                n += 1
            return n
        if name == "map_new":
            return {}
        if name == "read_file":
            # Deep-scan-19 fix (MEDIUM, TOCTOU): open the RESOLVED path
            # returned by _sandbox_check, not the original. Prevents a
            # race where a symlink inside the sandbox is swapped for one
            # pointing outside between the check and the open.
            resolved = _sandbox_check(args[0])
            try:
                with open(resolved, "rb") as f:
                    return f.read()
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line) from None
        # Stage 10-beta: read_file_tainted(path) — same as read_file but
        # the returned str is wrapped as tainted[str]. The wrapper dict
        # format is identical to taint_mark's output.
        if name == "read_file_tainted":
            resolved = _sandbox_check(args[0])
            try:
                with open(resolved, "rb") as f:
                    content = f.read()
                return {"tainted": True, "value": content}
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line) from None
        # Stage 10 release: read_line() -> tainted[str] — third taint source.
        # Reads one line from stdin (newline stripped). The result is always
        # tainted because stdin is untrusted input. EOF returns an empty
        # tainted string (mirrors fgets() semantics in the C runtime).
        if name == "read_line":
            raw = sys.stdin.buffer.readline()
            # Strip a trailing newline (matches the C runtime's hl_read_line).
            if raw.endswith(b"\n"):
                raw = raw[:-1]
                # Also strip a trailing \r if present (CRLF line endings).
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
            return {"tainted": True, "value": raw}
        if name == "write_file":
            resolved = _sandbox_check(args[0])
            try:
                with open(resolved, "wb") as f:
                    f.write(args[1])
                return None
            except OSError:
                raise HLPanic("cannot write file: %s" % to_display(args[0]), line) from None
        if name == "args":
            # BUG (deep-scan-5): this returned a fresh list COPY on every
            # call, but the native runtime returns THE process-global list
            # — mutating the result is observable in native code but
            # not under Stage-0 (a differential divergence). Return the
            # actual list so both implementations alias identically.
            return self.argv
        if name == "chr":
            if args[0] < 0 or args[0] > 255:
                raise HLPanic("chr out of range 0..255", line)
            return bytes([args[0]])
        if name == "clock_ms":
            return int(time.monotonic() * 1000)
        # ----- Stage 46 (v0.65.0-alpha): thread / scheduling builtins -----
        # thread_sleep_ms(ms) — block the calling thread for ms
        # milliseconds. Uses time.sleep (which yields the GIL).
        #
        # Note: we do NOT touch self.conc.blocked here. The deadlock
        # detector counts only threads blocked on CHANNEL operations
        # (chan.recv with no sender, chan.send on a full bounded
        # channel). A thread in time.sleep is NOT blocked on a
        # channel — it WILL wake up after the sleep duration — so
        # counting it as blocked would cause spurious deadlock
        # panics. The detector's contract is "no thread can ever
        # make progress", which is false while a sleep is pending.
        if name == "thread_sleep_ms":
            ms = args[0]
            if ms < 0:
                raise HLPanic("thread_sleep_ms: duration must be >= 0, "
                              "got %d" % ms, line)
            time.sleep(max(0.0, ms / 1000.0))
            return None
        # thread_yield() — hint the scheduler to switch. Python's
        # time.sleep(0) yields the GIL and lets another thread run.
        if name == "thread_yield":
            time.sleep(0)
            return None
        # thread_current_id() — a non-zero int identifying the
        # calling thread. Uses threading.get_ident() which returns
        # a non-zero int on CPython (the main thread gets a non-zero
        # ID; spawned threads get distinct non-zero IDs). The actual
        # VALUE is implementation-defined and may differ between
        # the interpreter and the native runtime — callers must
        # only rely on the property "different threads get different
        # IDs", not on any specific value.
        if name == "thread_current_id":
            return threading.get_ident()
        if name == "file_exists":
            # Deep-scan-15 cleanup: removed redundant `import os` —
            # `os` is already imported at module top (line 14), and
            # Python caches it in sys.modules so the local import was
            # a no-op besides raising a pylint W0404 reimport warning.
            # Deep-scan-19: use the RESOLVED path returned by _sandbox_check
            # (TOCTOU consistency with read_file / write_file).
            resolved = _sandbox_check(args[0])
            # Deep-scan fix (H2): pass bytes directly to os.path.isfile
            # (Python's os.path.isfile accepts bytes). The old code
            # decoded with errors="replace", which substituted U+FFFD
            # for non-UTF-8 bytes, so the interpreter checked a DIFFERENT
            # path than the native runtime (which passes raw bytes to
            # stat()). Files with non-UTF-8 names diverged.
            return os.path.isfile(resolved)
        # ----- Stage 36 (v0.55.0-alpha): filesystem metadata builtins -----
        # All four builtins route through _sandbox_check (so a sandboxed
        # interpreter can't escape) and use bytes-exact paths (so non-
        # UTF-8 names behave identically in the interpreter and the C
        # runtime — the same invariant the H2 deep-scan fix established
        # for file_exists).
        if name == "fs_read_dir":
            resolved = _sandbox_check(args[0])
            try:
                # os.listdir returns names (NOT full paths) — the
                # caller joins with the parent. Sort the result so
                # the interpreter and the C runtime (which uses
                # opendir/readdir) agree on the iteration order
                # (readdir order is filesystem-dependent; sort by
                # bytes for cross-implementation determinism).
                entries = sorted(os.listdir(resolved))
                # Each entry is a bytes object (because `resolved`
                # is bytes); return the list of bytes directly
                # (HLS str = Python bytes).
                return [e if isinstance(e, bytes)
                        else str(e).encode("utf-8") for e in entries]
            except OSError:
                raise HLPanic("fs_read_dir: cannot list directory: %s"
                              % to_display(args[0]), line) from None
        if name == "fs_size":
            resolved = _sandbox_check(args[0])
            try:
                return os.path.getsize(resolved)
            except OSError:
                raise HLPanic("fs_size: cannot stat: %s"
                              % to_display(args[0]), line) from None
        if name == "fs_is_dir":
            resolved = _sandbox_check(args[0])
            # os.path.isdir returns False for non-existent paths
            # (matches the C runtime's stat-based check: a missing
            # path is not a directory).
            return os.path.isdir(resolved)
        if name == "fs_set_perms":
            resolved = _sandbox_check(args[0])
            mode = args[1]
            try:
                os.chmod(resolved, mode)
            except OSError:
                raise HLPanic("fs_set_perms: cannot chmod: %s"
                              % to_display(args[0]), line) from None
            return None
        # ----- Stage 8-alpha: ownership primitives -----
        # drop(x): semantically releases x. In Stage-0 (Python), the underlying
        # value is left for Python's GC. The binding is marked moved at compile
        # time, so this runtime path just needs to be a no-op that returns None.
        if name == "drop":
            return None
        # clone(x): deep-copy a heap value.
        if name == "clone":
            return self.deep_clone(args[0])
        # take(x): returns x's value (binding is marked moved at compile time).
        if name == "take":
            return args[0]
        # ----- Stage 10-alpha: taint tracking -----
        # tainted_args() — like args() but each element is wrapped in the
        # `tainted[str]` runtime representation: a dict {"tainted": True,
        # "value": <bytes>}. The wrapper is created here; the std.taint /
        # std.sanitize helpers consume it. See std/taint.hls.
        if name == "tainted_args":
            return [{"tainted": True, "value": a} for a in self.argv]
        # taint_mark(x) — wrap any value as tainted. The wrapper is a dict
        # so it's distinguishable from raw values (especially strings,
        # which are bytes).
        if name == "taint_mark":
            return {"tainted": True, "value": args[0]}
        # taint_unwrap(x) — extract the inner value, dropping taint.
        # The checker rejects taint_unwrap on non-tainted values, so by
        # the time we get here, args[0] is guaranteed to be a tainted
        # wrapper dict.
        if name == "taint_unwrap":
            v = args[0]
            # Deep-scan-7 fix: the previous check `"tainted" in v` matched
            # any dict with a field literally named "tainted" — including
            # user structs with a `tainted: int` field. Tighten the check:
            # require BOTH "tainted" AND "value" keys, AND "tainted" must
            # be the boolean True (the taint-mark builtin sets it to True).
            if (isinstance(v, dict) and "tainted" in v and "value" in v
                    and v["tainted"] is True):
                return v["value"]
            # BUG-23 fix: the previous defensive fallback returned the
            # "value" field of ANY dict (including user structs that
            # happen to have a field named "value"). Since the checker
            # guarantees args[0] is a tainted[T], we panic if we get here
            # without the taint wrapper — that indicates a checker bug.
            raise HLPanic("taint_unwrap: expected tainted[T] wrapper, "
                          "got a non-tainted value (got %s)"
                          % type(v).__name__, line)
        # ----- Stage 9 release (v0.20.0-alpha): Net / Rand / Proc builtins -----
        # The interpreter implementations mirror the native runtime in
        # src/hlc.hls exactly, so differential testing passes.
        # rand_int(max: int) -> int — uniform random int in [0, max).
        # Panics on max <= 0 to keep the bound well-defined. Uses the
        # shared HalisRNG LCG so the sequence is identical to the native
        # runtime for the same seed.
        if name == "rand_int":
            if args[0] <= 0:
                raise HLPanic("rand_int() requires a positive max (got %d)"
                              % args[0], line)
            return self.rand_state.randrange(args[0])
        # rand_float() -> float — uniform random float in [0.0, 1.0).
        # Uses the same PRNG state as rand_int; 53 bits of randomness.
        if name == "rand_float":
            return self.rand_state.random()
        # rand_seed(s: int) -> void — seed the PRNG. Same seed produces
        # the same sequence in both the interpreter and the native
        # runtime (the constants and bit-masking are identical).
        if name == "rand_seed":
            self.rand_state.seed(args[0])
            return None
        # net_lookup(host: str) -> str — DNS resolution. Returns the
        # first IPv4 address as a string. Panics on failure (DNS error
        # or no A records). The interpreter uses Python's socket module
        # — the native runtime uses getaddrinfo directly.
        if name == "net_lookup":
            import socket
            host = args[0].decode("utf-8", "replace")
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET)
                for fam, _, _, _, sa in infos:
                    if fam == socket.AF_INET:
                        return sa[0].encode("ascii")
                raise HLPanic("net_lookup: no A records for %s"
                              % to_display(args[0]), line)
            except socket.gaierror as ex:
                raise HLPanic("net_lookup: DNS resolution failed for %s: %s"
                              % (to_display(args[0]), str(ex)), line) from None
            except OSError as ex:
                # Deep-scan fix (C2): connection timeouts, refused
                # connections, and other non-gaierror OSErrors used to
                # propagate as raw Python tracebacks while the native
                # runtime panicked cleanly. Catch the broader OSError
                # family for differential parity.
                raise HLPanic("net_lookup: network error for %s: %s"
                              % (to_display(args[0]), str(ex)), line) from None
        # ----- Stage 37 (v0.56.0-alpha): TCP / UDP / TLS builtins -----
        # The interpreter mirrors the C runtime exactly so differential
        # testing (interpreter == native) is byte-exact. All builtins
        # return / accept primitive int fd values; the stdlib wraps them
        # in TcpStream / TcpListener / UdpSocket structs.
        #
        # Socket fds are tracked in self.net_fds (a dict[int, socket])
        # so the interpreter can mirror close() and detect double-
        # close. The fd namespace is a monotonic counter starting at 1
        # (0 is never used; the C runtime uses 0/1/2 for stdin/stdout/
        # stderr, so a Halis fd of 0 would be ambiguous). Negative
        # values are errors.
        if name == "net_tcp_connect":
            import socket as _sock
            host = args[0].decode("utf-8", "replace")
            port = int(args[1])
            s = None
            try:
                infos = _sock.getaddrinfo(host, port, _sock.AF_INET,
                                          _sock.SOCK_STREAM)
                if not infos:
                    return -1
                fam, ty, pr, _, sa = infos[0]
                s = _sock.socket(fam, ty, pr)
                s.settimeout(5)
                s.connect(sa)
                return self._net_register(s)
            except OSError:
                # Deep-scan-24 fix: close the created socket when the
                # connect (or settimeout) fails. The old handler leaked
                # the fd — a program retrying refused connections in a
                # loop exhausted the process fd limit, while the native
                # runtime (closes the fd on failure) kept running. The
                # mirror must fail the same way AND leak nothing.
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
                return -1
        if name == "net_tcp_listen":
            import socket as _sock
            host = args[0].decode("utf-8", "replace")
            port = int(args[1])
            backlog = int(args[2])
            s = None
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
                s.bind((host, port))
                s.listen(backlog)
                return self._net_register(s)
            except OSError:
                # Deep-scan-24 fix: same leak as net_tcp_connect —
                # close the socket when bind/listen fails (e.g. port
                # already in use) instead of leaking the fd.
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
                return -1
        if name == "net_tcp_accept":
            fd = int(args[0])
            s = self.net_fds.get(fd)
            if s is None:
                return -1
            try:
                s.settimeout(30)
                conn, _addr = s.accept()
                return self._net_register(conn)
            except OSError:
                return -1
        if name == "net_read":
            fd = int(args[0])
            n = int(args[1])
            if n <= 0:
                return b""
            s = self.net_fds.get(fd)
            if s is None:
                return b""
            try:
                s.settimeout(5)
                data = s.recv(n)
                return data if data else b""
            except OSError:
                return b""
        if name == "net_write":
            fd = int(args[0])
            data = args[1]
            s = self.net_fds.get(fd)
            if s is None:
                return -1
            try:
                s.settimeout(5)
                # sendall blocks until all bytes are written (or the
                # socket errors). Return the number of bytes written
                # (== len(data) on success). On error return -1.
                s.sendall(data)
                return len(data)
            except OSError:
                return -1
        if name == "net_close":
            fd = int(args[0])
            s = self.net_fds.pop(fd, None)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            return None
        if name == "net_udp_open":
            import socket as _sock
            s = None
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                s.settimeout(5)
                return self._net_register(s)
            except OSError:
                # Deep-scan-24 fix: close on failure (settimeout/register
                # error) instead of leaking the fd.
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
                return -1
        if name == "net_udp_send_to":
            fd = int(args[0])
            host = args[1].decode("utf-8", "replace")
            port = int(args[2])
            data = args[3]
            s = self.net_fds.get(fd)
            if s is None:
                return -1
            try:
                import socket as _sock
                infos = _sock.getaddrinfo(host, port, _sock.AF_INET,
                                          _sock.SOCK_DGRAM)
                if not infos:
                    return -1
                _f, _t, _p, _c, sa = infos[0]
                n = s.sendto(data, sa)
                return n
            except OSError:
                return -1
        if name == "net_udp_recv_from":
            fd = int(args[0])
            n = int(args[1])
            if n <= 0:
                return b""
            s = self.net_fds.get(fd)
            if s is None:
                return b""
            try:
                s.settimeout(5)
                data, _addr = s.recvfrom(n)
                return data if data else b""
            except OSError:
                return b""
        if name == "net_tls_get":
            # HTTPS GET via libcurl. The interpreter shells out to the
            # `curl` command-line tool (universally available on dev
            # machines) to avoid requiring the Python pycurl binding.
            # The native runtime links libcurl directly. Both produce
            # the same response body byte-for-byte for the same URL.
            #
            # Build the URL: https://host:port/path
            host = args[0].decode("utf-8", "replace")
            port = int(args[1])
            path = args[2].decode("utf-8", "replace")
            if len(path) == 0 or path[0:1] != "/":
                path = "/" + path
            url = "https://" + host + ":" + str(port) + path
            try:
                # -s silent, -S show errors, -L follow redirects,
                # --max-time 30 (avoid hanging on slow networks),
                # --fail panic on HTTP >= 400.
                proc = subprocess.run(
                    ["curl", "-sS", "-L", "--max-time", "30",
                     "--fail", url],
                    capture_output=True, timeout=35)
                if proc.returncode != 0:
                    err = proc.stderr.decode("utf-8", "replace")
                    raise HLPanic("net_tls_get: curl failed for %s: %s"
                                  % (url, err), line)
                body = proc.stdout
                return body if isinstance(body, bytes) else body.encode("utf-8")
            except FileNotFoundError:
                raise HLPanic("net_tls_get: curl not installed (libcurl "
                              "backend requires the curl CLI for the "
                              "interpreter; the native runtime links "
                              "libcurl directly)", line) from None
            except subprocess.TimeoutExpired:
                raise HLPanic("net_tls_get: timeout for %s" % url, line) from None
        # proc_exec(cmd: str) -> int — run a shell command. Returns the
        # exit code (0 on success, 1..255 on failure). Uses os.system()
        # so the command runs in a subshell, matching the C runtime's
        # system() call. Tainted command strings are rejected at
        # check time (proc_exec is a taint sink for argument 0).
        if name == "proc_exec":
            # Deep-scan-15 cleanup: removed redundant `import os` (see
            # the file_exists branch above for the rationale).
            # Stage 53 deep-scan-22 fix (MEDIUM severity): decode with
            # `surrogateescape` (not `replace`) so non-UTF-8 command
            # bytes round-trip through os.system back to the EXACT same
            # bytes the C runtime's system() receives. With `replace`,
            # byte 0xff became U+FFFD, which os.system re-encoded to
            # the 3-byte UTF-8 sequence \xef\xbf\xbd — so the shell
            # saw DIFFERENT command bytes in the interpreter vs the
            # native binary (a differential mismatch for any proc_exec
            # command containing non-UTF-8 bytes, e.g. from chr(255)).
            # `surrogateescape` maps each invalid byte to a lone
            # surrogate (U+DC80..U+DCFF); os.system uses the filesystem
            # encoding (utf-8/surrogateescape on Linux), which restores
            # the original byte verbatim.
            cmd = args[0].decode("utf-8", "surrogateescape")
            # Deep-scan-26 fix (interpreter-side mirror of the native
            # fflush(stdout) in hl_proc_exec): flush self.out BEFORE the
            # child inherits fd 1, so the parent's pending println output
            # keeps its logical position. Python 3.12+ flushes stdout in
            # os.system() itself, but 3.8/3.9/3.10/3.11 do NOT — without
            # this flush the child's output jumped BEFORE the whole
            # buffered program output on those versions, a differential
            # mismatch (feat_deep_scan22_boot failed on the CI 3.11 leg
            # for exactly this reason).
            self.out.flush()
            rc = os.system(cmd)
            # Deep-scan-7 fix: os.WIFEXITED / WEXITSTATUS / WTERMSIG
            # are POSIX-only macros. On Windows, os.system returns the
            # exit code directly (not a status word). Detect the
            # platform and handle both cases so the interpreter
            # produces the same result as the C runtime on every OS.
            if sys.platform == "win32":
                # Windows: rc is already the exit code (0..255).
                # Encode signal-like values as 128 + signum for parity.
                if rc < 0:
                    return 128 + (-rc)
                return rc & 0xFF
            # POSIX: os.system returns a status word; the exit code is
            # the high byte (WEXITSTATUS).
            if os.WIFEXITED(rc):
                return os.WEXITSTATUS(rc)
            # Killed by signal — encode as 128 + signum, like shells.
            return 128 + os.WTERMSIG(rc)
        # ----- Stage 47 (v0.66.0-alpha): process management builtins -----
        # The interpreter mirrors the C runtime exactly so differential
        # testing (interpreter == native) is byte-exact. Child processes
        # are tracked in self.proc_children (a dict[int, subprocess.Popen])
        # keyed by an int pid (a monotonic counter starting at 1, NOT
        # the OS pid — the OS pid differs between interpreter and native,
        # so we use our own namespace). The Halis-level pid is what
        # proc_wait / proc_kill / proc_child_* use; the runtime translates
        # it to the OS pid internally.
        if name == "proc_spawn":
            # Stage 53 deep-scan-22 fix (MEDIUM severity): decode with
            # `surrogateescape` (not `replace`) so non-UTF-8 program
            # names / arg bytes round-trip through subprocess.Popen
            # back to the EXACT bytes the C runtime's execv receives.
            # With `replace`, byte 0xff became U+FFFD, which Popen
            # re-encoded to the 3-byte UTF-8 sequence — the child saw
            # DIFFERENT argv bytes in the interpreter vs the native
            # binary (same class of bug as proc_exec).
            program = args[0].decode("utf-8", "surrogateescape")
            arg_list = args[1]  # list of bytes
            stdin_kind = int(args[2])
            stdout_kind = int(args[3])
            stderr_kind = int(args[4])
            if stdin_kind < 0 or stdin_kind > 2:
                raise HLPanic("proc_spawn: stdin_kind must be 0/1/2", line)
            if stdout_kind < 0 or stdout_kind > 2:
                raise HLPanic("proc_spawn: stdout_kind must be 0/1/2", line)
            if stderr_kind < 0 or stderr_kind > 2:
                raise HLPanic("proc_spawn: stderr_kind must be 0/1/2", line)
            # Translate stdio kinds to subprocess constants.
            def _translate_stdio(kind):
                if kind == 0:
                    return None  # inherit
                if kind == 1:
                    return subprocess.PIPE
                return subprocess.DEVNULL  # null
            try:
                argv = [program] + [a.decode("utf-8", "surrogateescape") for a in arg_list]
                # Deep-scan-26 fix (interpreter-side mirror of the native
                # fflush(stdout) before fork() in hl_proc_spawn): flush
                # self.out BEFORE spawning so an inheriting child
                # (stdout_kind 0) cannot jump ahead of the parent's
                # buffered println output on Python < 3.12.
                self.out.flush()
                proc = subprocess.Popen(
                    argv,
                    stdin=_translate_stdio(stdin_kind),
                    stdout=_translate_stdio(stdout_kind),
                    stderr=_translate_stdio(stderr_kind),
                    close_fds=True,
                )
            except FileNotFoundError:
                raise HLPanic("proc_spawn: program not found: %s"
                              % to_display(args[0]), line) from None
            except OSError as ex:
                raise HLPanic("proc_spawn: %s: %s"
                              % (to_display(args[0]), str(ex)), line) from None
            # Allocate a Halis-level pid (monotonic counter, starting at 1).
            if not hasattr(self, "proc_next_pid") or self.proc_next_pid < 1:
                self.proc_next_pid = 1
            if not hasattr(self, "proc_children"):
                self.proc_children = {}
            hl_pid = self.proc_next_pid
            self.proc_next_pid += 1
            self.proc_children[hl_pid] = proc
            return hl_pid
        if name == "proc_wait":
            hl_pid = int(args[0])
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_wait: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            try:
                rc = proc.wait()
            except OSError:
                return -1
            # rc is already the exit code (subprocess encodes signals
            # as -N; translate to 128 + signum for parity with proc_exec).
            if rc < 0:
                return 128 + (-rc)
            return rc & 0xFF
        if name == "proc_kill":
            hl_pid = int(args[0])
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_kill: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            try:
                proc.terminate()
                return 0
            except OSError:
                return -1
        if name == "proc_child_write":
            hl_pid = int(args[0])
            data = args[1]
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_child_write: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            if proc.stdin is None:
                return -1
            try:
                proc.stdin.write(data if isinstance(data, bytes)
                                 else bytes(data))
                proc.stdin.flush()
                return len(data)
            except (OSError, BrokenPipeError, ValueError):
                return -1
        if name == "proc_child_read":
            hl_pid = int(args[0])
            fd_kind = int(args[1])
            n = int(args[2])
            if fd_kind not in (1, 2):
                raise HLPanic("proc_child_read: fd_kind must be 1 (stdout) "
                              "or 2 (stderr), got %d" % fd_kind, line)
            if n < 0:
                raise HLPanic("proc_child_read: n must be >= 0, got %d" % n, line)
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_child_read: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            stream = proc.stdout if fd_kind == 1 else proc.stderr
            if stream is None:
                return b""
            try:
                return stream.read(n) if n > 0 else b""
            except OSError:
                return b""
        if name == "proc_child_close":
            hl_pid = int(args[0])
            fd_kind = int(args[1])
            if fd_kind not in (0, 1, 2):
                raise HLPanic("proc_child_close: fd_kind must be 0/1/2, "
                              "got %d" % fd_kind, line)
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_child_close: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            stream = {0: proc.stdin, 1: proc.stdout, 2: proc.stderr}[fd_kind]
            if stream is None:
                return 0  # idempotent on already-closed / not-opened
            try:
                stream.close()
                return 0
            except OSError:
                return -1
        # ----- Stage 48 (v0.67.0-alpha): environment + cwd builtins -----
        # The interpreter uses Python's os.environ, os.getcwd, os.chdir.
        # All return / consume bytes-exact str (HLS str = Python bytes)
        # so non-UTF-8 env vars and cwd paths behave identically to the
        # C runtime (which uses getenv / setenv / unsetenv / getcwd /
        # chdir with raw char*).
        #
        # These are LOW-LEVEL builtins returning plain str / bool / etc.
        # The stdlib std/env.hls wrapper applies the taint wrappers to
        # produce the user-facing API (env_var -> Option[tainted[str]],
        # env_current_dir -> tainted[str], env_args_os -> list[tainted[str]]).
        if name == "env_get":
            # Stage 53 deep-scan-22 fix (HIGH severity): use
            # `surrogateescape` (not `replace`) for both key decode and
            # value encode so non-UTF-8 env vars round-trip byte-exact.
            # The previous `replace` decode lost the original key bytes
            # (a non-UTF-8 key became U+FFFD, so the lookup failed) AND
            # the `encode("utf-8")` crashed with UnicodeEncodeError on
            # env values containing surrogates (Python's os.environ
            # decodes env vars with surrogateescape, so a raw-byte env
            # value like b"\xff" becomes '\udcff', which utf-8 cannot
            # re-encode). The C runtime's getenv returns the raw bytes
            # — this fix makes the boot match.
            key = args[0].decode("utf-8", "surrogateescape")
            val = os.environ.get(key, "")
            return val.encode("utf-8", "surrogateescape") if isinstance(val, str) else bytes(val)
        if name == "env_has":
            # Stage 53 deep-scan-22 fix (MEDIUM severity): surrogateescape
            # so a non-UTF-8 key looks up correctly (replace would map
            # every invalid byte to U+FFFD, mismatching the actual key).
            key = args[0].decode("utf-8", "surrogateescape")
            return key in os.environ
        if name == "env_set":
            # Stage 53 deep-scan-22 fix (MEDIUM severity): surrogateescape
            # on both key and value so non-UTF-8 bytes are stored verbatim
            # (matching the C setenv, which takes raw char*).
            key = args[0].decode("utf-8", "surrogateescape")
            val = args[1].decode("utf-8", "surrogateescape")
            os.environ[key] = val
            return None
        if name == "env_unset":
            # Stage 53 deep-scan-22 fix (MEDIUM severity): surrogateescape
            # so a non-UTF-8 key is matched and removed correctly.
            key = args[0].decode("utf-8", "surrogateescape")
            if key in os.environ:
                del os.environ[key]
            return None
        if name == "cwd_get":
            # Stage 53 deep-scan-22 fix (HIGH severity): encode with
            # surrogateescape so a non-UTF-8 cwd path round-trips
            # byte-exact. os.getcwd() on Linux returns a str decoded
            # with surrogateescape; encoding with plain utf-8 would
            # crash on any non-UTF-8 path component.
            cwd = os.getcwd()
            return cwd.encode("utf-8", "surrogateescape") if isinstance(cwd, str) else bytes(cwd)
        if name == "cwd_set":
            # Stage 53 deep-scan-22 fix (MEDIUM severity): surrogateescape
            # so a non-UTF-8 path is passed to chdir verbatim (the C
            # runtime's chdir takes raw char*).
            path = args[0].decode("utf-8", "surrogateescape")
            try:
                os.chdir(path)
                return 0
            except OSError:
                return -1
        if name == "args_os":
            # Same as args() — the OS-string version. The roadmap
            # distinguishes args_os from tainted_args semantically
            # (args_os is the raw bytes from the OS; tainted_args is
            # the logical str version) but in Halis str = bytes
            # already, so they currently alias.
            return self.argv
        # ----- Stage 49 (v0.68.0-alpha): high-resolution time builtins -----
        # instant_now_ns() — monotonic nanoseconds. Python's
        # time.monotonic_ns() returns an int that never decreases
        # (modulo wrap at int64 — ~292 years). Used by Instant::now().
        if name == "instant_now_ns":
            return int(time.monotonic_ns())
        # system_time_now_ms() — wall-clock milliseconds since the
        # Unix epoch (1970-01-01 UTC). Python's time.time() returns a
        # float; we multiply by 1000 and truncate to int. The wall
        # clock can jump on NTP adjustments.
        if name == "system_time_now_ms":
            return int(time.time() * 1000.0)
        # ----- Stage 50 (v0.69.0-alpha): libm-backed math builtins -----
        # All delegate to Python's math module (which wraps libm on
        # CPython). NaN/Inf/signed-zero/subnormal handling matches
        # IEEE-754 because Python's float is C double, and math.*
        # calls libm directly. The native Halis runtime links -lm
        # and calls the same libm functions, so differential parity
        # (interpreter == native) holds bit-for-bit on the same
        # platform (libm is platform-consistent on Linux x86-64 /
        # aarch64 / riscv64 — we don't claim cross-platform parity).
        # Deep-scan-20 fix (HIGH, interpreter/native parity): Python's
        # math module raises ValueError on domain errors and
        # OverflowError on range errors, where the C runtime (hlc.hls
        # emits raw libm calls) returns NaN / ±inf. Uncaught, these
        # killed the process with a raw Python traceback and exit 1
        # while the native build printed a value and kept running.
        # Map the raising family onto libm semantics.
        def _libm(f, *a, **kw):
            try:
                return f(*a)
            except ValueError:
                # Domain error: C returns the hardware default QNaN
                # (sign bit set on x86-64 — deep-scan-25 parity fix;
                # poles overridden by callers that pass on_domain=...).
                return kw.get("on_domain", _NAN_DEFAULT)
            except OverflowError:
                # Range error: C returns ±HUGE_VAL.
                return math.copysign(float("inf"), kw.get("signof", 1.0))

        if name == "math_sin":
            return math.sin(args[0])
        if name == "math_cos":
            return math.cos(args[0])
        if name == "math_tan":
            return math.tan(args[0])
        if name == "math_asin":
            # Deep-scan-25: glibc's asin/acos invalid-input path returns a
            # POSITIVE NaN (via __math_invalid), unlike sqrt/log/fmod whose
            # domain errors yield the sign-bit-set default QNaN on x86-64.
            return _libm(math.asin, args[0], on_domain=float("nan"))
        if name == "math_acos":
            return _libm(math.acos, args[0], on_domain=float("nan"))
        if name == "math_atan":
            return math.atan(args[0])
        if name == "math_atan2":
            return math.atan2(args[0], args[1])
        if name == "math_sinh":
            return _libm(math.sinh, args[0], signof=args[0])
        if name == "math_cosh":
            return _libm(math.cosh, args[0])
        if name == "math_tanh":
            return math.tanh(args[0])
        if name == "math_exp":
            return _libm(math.exp, args[0])
        if name == "math_log":
            # C pole error: log(0) / log(-0.0) = -inf, not NaN.
            if args[0] == 0:
                return float("-inf")
            return _libm(math.log, args[0])
        if name == "math_log10":
            if args[0] == 0:
                return float("-inf")
            # Deep-scan-25: glibc log10's domain error returns a POSITIVE
            # NaN (same __math_invalid path as asin/acos) — override.
            return _libm(math.log10, args[0], on_domain=float("nan"))
        if name == "math_log2":
            if args[0] == 0:
                return float("-inf")
            return _libm(math.log2, args[0])
        if name == "math_pow":
            # Deep-scan-24 fix (interpreter/native parity): C99 Annex F
            # pole rule — pow(+-0, y<0) returns +-inf (the sign follows
            # the zero's sign when y is an odd integer, +inf otherwise),
            # NOT NaN. Python's math.pow raises ValueError at the pole
            # and _libm mapped that to NaN, so the interpreter printed
            # `nan` where the native build printed `inf` / `-inf`
            # (verified against glibc). Mirror the C rule exactly.
            if args[0] == 0 and args[1] < 0:
                if args[1] == math.floor(args[1]) and int(args[1]) % 2 != 0:
                    return math.copysign(float("inf"), args[0])
                return float("inf")
            # Estimate the C result's sign for range errors: only a
            # negative base with an odd integral exponent overflows
            # to -inf; everything else overflows to +inf.
            _sg = 1.0
            if args[0] < 0 and float(args[1]).is_integer() and int(args[1]) % 2 == 1:
                _sg = -1.0
            return _libm(math.pow, args[0], args[1], signof=_sg)
        if name == "math_sqrt":
            return _libm(math.sqrt, args[0])
        if name == "math_cbrt":
            # Python's math module added cbrt in 3.11; we require
            # 3.12+ (boot.py guard), so it's available.
            return math.cbrt(args[0])
        if name == "math_hypot":
            return math.hypot(args[0], args[1])
        if name == "math_fmod":
            # C: fmod(x, 0) is a domain error -> NaN.
            return _libm(math.fmod, args[0], args[1])
        if name == "math_erf":
            return math.erf(args[0])
        if name == "math_erfc":
            return math.erfc(args[0])
        if name == "math_tgamma":
            # Deep-scan-24 fix (interpreter/native parity): C99 Annex F
            # pole rule — tgamma(+-0) returns +-inf (sign follows the
            # zero's sign: tgamma(+0)=+inf, tgamma(-0)=-inf), NOT NaN.
            # Python's math.gamma raises ValueError at the pole and
            # _libm mapped that to NaN; the native build (raw libm)
            # prints `inf` / `-inf` (verified against glibc).
            if args[0] == 0:
                return math.copysign(float("inf"), args[0])
            return _libm(math.gamma, args[0])
        if name == "math_lgamma":
            # C poles (0, -1, -2, ...) yield +inf, not NaN.
            return _libm(math.lgamma, args[0], on_domain=float("inf"))
        if name == "math_isnan":
            return math.isnan(args[0])
        if name == "math_isinf":
            return math.isinf(args[0])
        if name == "math_isfinite":
            return math.isfinite(args[0])
        if name == "math_signbit":
            # Python's math.copysign(1.0, x) returns 1.0 for positive
            # (incl +0.0) and -1.0 for negative (incl -0.0). signbit
            # is true iff the sign bit is set (negative or -0.0).
            return math.copysign(1.0, args[0]) < 0.0
        if name == "math_copysign":
            return math.copysign(args[0], args[1])
        # ----- Stage 16 (v0.27.0-alpha): concurrency builtins -----
        # chan_new() -> Chan[T] — a fresh, empty channel.
        if name == "chan_new":
            ch = HLChan()
            self.conc.register(ch)
            return ch
        # chan_new_bounded(cap: int) -> Chan[T] — a fresh bounded channel
        # (Stage-16 perfection, v0.29.0-alpha): send blocks while the
        # channel holds `cap` messages (backpressure). The checker
        # rejects literal capacities < 1; dynamic ones are validated here.
        if name == "chan_new_bounded":
            cap = args[0]
            if cap < 1:
                raise HLPanic(
                    "chan_new_bounded() capacity must be >= 1, got %d" % cap,
                    line)
            ch = HLChan(cap)
            self.conc.register(ch)
            return ch
        # select(chs: list[Chan[T]]) -> int — block until any channel is
        # ready; return the index (in list order) of the first ready one.
        if name == "select":
            chans = args[0]
            return self.conc.select(chans, line)
        # ----- Stage 33 (v0.52.0-alpha): async/await builtins -----
        # await(fut: Future[T]) -> T — block until the future is ready,
        # return its value. The Future is a cap-1 bounded HLChan; await
        # is just chan.recv() on it.
        if name == "await":
            fut = args[0]
            return self.conc.recv(fut, line)
        # future_ready(v: T) -> Future[T] — make an immediately-ready
        # future. Create a cap-1 chan and send the value; the recv()
        # in await() will find it instantly.
        if name == "future_ready":
            # Deep-scan-25 fix (soundness, interpreter/native parity):
            # the value crosses a task boundary at await(), so — exactly
            # like chan.send and the native gen_own_arg — an owned value
            # must be deep-copied here unless the argument is
            # syntactically clone(...) (already a private copy). The
            # old code sent the CALLER'S list/dict by reference: the
            # producer could mutate it after future_ready() and the
            # consumer would observe the mutation (a cross-thread data
            # race the Send/boundary system exists to prevent).
            v = args[0]
            is_clone = False
            if arg_nodes:
                a0 = arg_nodes[0]
                if a0.get("k") == "call" and a0.get("name") == "clone":
                    is_clone = True
            if not is_clone and isinstance(v, (list, dict)):
                v = self.deep_clone(v)
            ch = HLChan(1)
            self.conc.register(ch)
            self.conc.send(ch, v)
            return ch
        # future_poll(fut: Future[T]) -> Option[T] — non-blocking poll.
        # Returns Some(v) if the future is ready, None otherwise.
        if name == "future_poll":
            fut = args[0]
            with self.conc.cv:
                if fut.q:
                    v = fut.q.pop(0)
                    self.conc.msgs -= 1
                    self.conc.cv.notify_all()
                    return {"enum": "Option", "var": "Some", "data": [v]}
            return {"enum": "Option", "var": "None", "data": []}
        # future_select(futs: list[Future[T]]) -> int — race multiple
        # futures; return the index of the first ready one. Same as
        # select() but on Futures (which are channels under the hood).
        if name == "future_select":
            futs = args[0]
            return self.conc.select(futs, line)
        # ----- Stage 34 (v0.53.0-alpha): async stream builtins -----
        # stream_new(cap: int) -> Stream[T] — bounded stream.
        if name == "stream_new":
            cap = args[0]
            if cap < 1:
                raise HLPanic(
                    "stream_new() capacity must be >= 1, got %d" % cap,
                    line)
            ch = HLChan(cap)
            self.conc.register(ch)
            return ch
        # stream_send(s: Stream[T], v: T) — push a value (backpressure).
        if name == "stream_send":
            s = args[0]
            v = args[1]
            # Boundary ownership: deep-clone owned values unless fresh.
            # Deep-scan-25 fix (soundness, interpreter/native parity):
            # the comment previously claimed "the checker already
            # rejected bare ident reads; if we reach here the value is
            # a primitive, a clone(...) result, or a fresh expression
            # result" — but a METHOD-call result like xs.get(0) is an
            # alias into the caller's list, and the checker's rule only
            # rejects ident/field/index nodes. chan.send and the native
            # codegen (gen_own_arg) deep-copy at this boundary; mirror
            # them exactly (same clone(...) exemption as chan.send).
            is_clone = False
            if arg_nodes:
                a1 = arg_nodes[1]
                if a1.get("k") == "call" and a1.get("name") == "clone":
                    is_clone = True
            if not is_clone and isinstance(v, (list, dict)):
                v = self.deep_clone(v)
            self.conc.send(s, v)
            return None
        # stream_recv(s: Stream[T]) -> T — block until a value is available.
        if name == "stream_recv":
            s = args[0]
            return self.conc.recv(s, line)
        # stream_try_recv(s: Stream[T], default: T) -> T — non-blocking.
        if name == "stream_try_recv":
            s = args[0]
            default = args[1]
            return self.conc.recv_or(s, default)
        # stream_len(s: Stream[T]) -> int — pending message count.
        if name == "stream_len":
            s = args[0]
            with self.conc.cv:
                return len(s.q)
        # stream_close(s: Stream[T]) — signal end-of-stream.
        # For int streams, send INT64_MIN (the sentinel). For other
        # element types, this is a no-op (the user must send the
        # appropriate sentinel via stream_send). The builtin exists so
        # the type checker can validate the user remembered to close.
        # We send the int sentinel regardless — it's harmless on
        # non-int streams (the value is just a sentinel the consumer
        # checks for, and non-int consumers don't check).
        if name == "stream_close":
            s = args[0]
            self.conc.send(s, INT64_MIN_SENTINEL)
            return None
        # stream_take_int(in_s, n) -> Stream[int] — create an output
        # stream, spawn a worker that forwards the first n values then
        # sends the sentinel. The checker did NOT rewrite this node
        # (no fn-name argument), so we handle it here directly.
        if name == "stream_take_int":
            in_s = args[0]
            n = args[1]
            out_s = HLChan(16)
            self.conc.register(out_s)
            conc = self.conc
            interp = self

            def take_runner():
                try:
                    count = 0
                    while count < n:
                        v = conc.recv(in_s, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            break
                        conc.send(out_s, v)
                        count = count + 1
                    conc.send(out_s, INT64_MIN_SENTINEL)
                except HLPanic as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (at line %d)\n"
                                     % (to_display(ex.msg), ex.line))
                    os._exit(101)
                except BaseException as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (in stream_take)\n" % ex)
                    os._exit(101)
                with conc.cv:
                    conc.tasks_alive -= 1
                    conc.cv.notify_all()

            with self.conc.cv:
                self.conc.tasks_alive += 1
            t = threading.Thread(target=take_runner)
            t.daemon = True
            t.start()
            return out_s
        # stream_merge_int(a, b) -> Stream[int] — interleave two streams.
        if name == "stream_merge_int":
            a = args[0]
            b = args[1]
            out_s = HLChan(16)
            self.conc.register(out_s)
            conc = self.conc
            interp = self

            def merge_runner():
                try:
                    a_done = False
                    b_done = False
                    while not a_done or not b_done:
                        if not a_done:
                            v = conc.recv(a, interp.line)
                            if v == INT64_MIN_SENTINEL:
                                a_done = True
                            else:
                                conc.send(out_s, v)
                        if not b_done:
                            v = conc.recv(b, interp.line)
                            if v == INT64_MIN_SENTINEL:
                                b_done = True
                            else:
                                conc.send(out_s, v)
                    conc.send(out_s, INT64_MIN_SENTINEL)
                except HLPanic as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (at line %d)\n"
                                     % (to_display(ex.msg), ex.line))
                    os._exit(101)
                except BaseException as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (in stream_merge)\n" % ex)
                    os._exit(101)
                with conc.cv:
                    conc.tasks_alive -= 1
                    conc.cv.notify_all()

            with self.conc.cv:
                self.conc.tasks_alive += 1
            t = threading.Thread(target=merge_runner)
            t.daemon = True
            t.start()
            return out_s
        raise HLPanic("unknown builtin function: %s" % name, line)

    # ---------- Stage 16: spawn ----------
