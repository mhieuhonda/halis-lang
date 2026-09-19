"""hlserve_hmr — Stage 75 WebSocket-based HMR message bus.

Replaces the Stage 24 SSE endpoint with a proper RFC 6455 WebSocket
server. Rationale:

* SSE is one-way (server -> client). Stage 75 wants the browser to be
  able to push messages back: client-side ``console.log`` forwarding
  (so stdout from the wasm program shows up in the dev server's
  terminal, not just the browser console), client-side errors
  (uncaught exceptions, runtime panics), and a ``identify`` message
  (the browser tells the server its tab-id so multiple tabs of the
  same app each get their own stats counter).
* SSE is text-only. Stage 75 may eventually want to push binary
  deltas (a small wasm hot-patch). WebSocket handles binary natively.
* SSE has no built-in close handshake; if the tab navigates away,
  the server keeps the connection half-open until a TCP keepalive
  timeout. WebSocket has a clean ``close`` opcode.

This module implements the WebSocket protocol by hand (Python stdlib
has no built-in WS server). The handshake + frame encode/decode is
~120 lines; the HMR protocol on top is another ~80.

Wire format (JSON text frames, opcode 0x1):

  Server -> Client:
    {type: "hello",        version: 1, server: "hls-serve v..."}
    {type: "ok",           stats: {...}}
    {type: "errors",       errors: [...]}
    {type: "warnings",     warnings: [...]}
    {type: "reload",       reason: "..."}                  # full page reload
    {type: "hot-reload",   wasm_url: "..."}                # wasm module swap
    {type: "progress",     phase: "...", percent: 0..100}
    {type: "connected",    client_id: 1, n_clients: 1}
    {type: "pong"}                                              # response to ping

  Client -> Server:
    {type: "identify",     client: "browser", id: "..."}
    {type: "log",          level: "log|warn|error", args: [...]}
    {type: "error",        message: "...", stack: "..."}
    {type: "ping"}
    {type: "pong"}
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import threading
import time
from typing import Callable, List, Optional

from hlserve_common import HMR_PROTOCOL_VERSION, SERVER_BANNER, debug


# The RFC 6455 magic GUID the server appends to the client's key
# before SHA-1 + base64.
WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


# ---------------------------------------------------------------------------
# Frame encode / decode
# ---------------------------------------------------------------------------

def ws_handshake_response(ws_key: str) -> bytes:
    """Build the HTTP/1.1 101 response for a WebSocket handshake.

    ``ws_key`` is the ``Sec-WebSocket-Key`` header value (a 24-char
    base64 string from the browser). The response includes the
    ``Sec-WebSocket-Accept`` header computed per RFC 6455 §4.2.2:
    ``base64(sha1(ws_key + GUID))``.
    """
    digest = hashlib.sha1(ws_key.encode("ascii") + WS_MAGIC).digest()
    accept = base64.b64encode(digest).decode("ascii")
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n"
        b"\r\n"
    )


def ws_encode_frame(payload: bytes,
                    opcode: int = OP_TEXT,
                    mask: bool = False) -> bytes:
    """Encode a single WebSocket frame.

    Server -> client frames MUST NOT be masked (RFC 6455 §5.3).
    Client -> server frames MUST be masked. We support both (the
    server uses mask=False; we use mask=True only when sending ping
    frames to test the encode path).
    """
    fin = 0x80  # FIN bit; we don't fragment.
    b0 = fin | (opcode & 0x0F)
    out = bytearray([b0])
    plen = len(payload)
    if plen < 126:
        b1 = (0x80 if mask else 0x00) | plen
        out.append(b1)
    elif plen < 65536:
        b1 = (0x80 if mask else 0x00) | 126
        out.append(b1)
        out.extend(struct.pack(">H", plen))
    else:
        b1 = (0x80 if mask else 0x00) | 127
        out.append(b1)
        out.extend(struct.pack(">Q", plen))
    if mask:
        mask_key = os.urandom(4)
        out.extend(mask_key)
        masked = bytearray(len(payload))
        for i, b in enumerate(payload):
            masked[i] = b ^ mask_key[i % 4]
        out.extend(masked)
    else:
        out.extend(payload)
    return bytes(out)


# Deep-scan-28: hard cap on one WebSocket frame's payload. A hostile
# frame advertises plen up to 2^64-1 and _recv_exact would try to buffer
# it all — a trivial memory-exhaustion DoS from any local process (the
# std websocket module caps at ws_max_payload_size; the dev server now
# does too). 16 MiB is far above anything the HMR protocol sends
# (frames are < 126 bytes in practice).
WS_MAX_FRAME_PAYLOAD = 16 * 1024 * 1024


def ws_read_frame(sock) -> Optional[tuple]:
    """Read ONE WebSocket frame from ``sock`` (a Python socket).

    Returns ``(fin, opcode, payload_bytes)`` or ``None`` if the
    connection was closed cleanly (a CLOSE frame or EOF).

    Handles fragmentation (a frame with FIN=0 is a continuation; the
    caller is responsible for accumulating). In practice the HMR
    protocol uses small text frames (< 126 bytes payload), so we never
    actually fragment.
    """
    hdr = _recv_exact(sock, 2)
    if hdr is None:
        return None
    b0, b1 = hdr[0], hdr[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    plen = b1 & 0x7F
    if plen == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        plen = struct.unpack(">H", ext)[0]
    elif plen == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        plen = struct.unpack(">Q", ext)[0]
    if plen > WS_MAX_FRAME_PAYLOAD:
        # Refuse absurd frames instead of buffering them (DoS guard).
        return None
    mask_key = None
    if masked:
        mask_key = _recv_exact(sock, 4)
        if mask_key is None:
            return None
    if plen == 0:
        payload = b""
    else:
        payload = _recv_exact(sock, plen)
        if payload is None:
            return None
        if masked and mask_key is not None:
            ba = bytearray(plen)
            for i in range(plen):
                ba[i] = payload[i] ^ mask_key[i % 4]
            payload = bytes(ba)
    return (fin, opcode, payload)


def _recv_exact(sock, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from ``sock``. Returns None on EOF."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (OSError, ConnectionResetError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# HMR client + bus
# ---------------------------------------------------------------------------

class HmrClient:
    """One connected browser tab. Has its own send-lock so concurrent
    ``broadcast()`` calls from the server don't interleave a single
    client's frames."""

    def __init__(self, client_id: int, sock, addr):
        self.id = client_id
        self.sock = sock
        self.addr = addr
        self.alive = True
        self.send_lock = threading.Lock()
        self.ident = None  # the client's "identify" payload (id, etc.)
        self.connected_at = time.time()

    def send_json(self, obj: dict) -> bool:
        """Send a JSON message. Returns False if the client is gone."""
        if not self.alive:
            return False
        text = json.dumps(obj, ensure_ascii=False)
        frame = ws_encode_frame(text.encode("utf-8"), opcode=OP_TEXT)
        with self.send_lock:
            try:
                self.sock.sendall(frame)
                return True
            except (OSError, BrokenPipeError):
                self.alive = False
                return False

    def send_close(self) -> None:
        if not self.alive:
            return
        try:
            self.sock.sendall(ws_encode_frame(b"", opcode=OP_CLOSE))
        except OSError:
            pass
        self.alive = False


class HmrBus:
    """The HMR message bus. Holds the list of connected clients and
    broadcasts JSON messages to all of them.

    Threading: ``broadcast`` is safe to call from any thread (the
    compile callback runs in the watcher thread). ``subscribe`` /
    ``unsubscribe`` are guarded by a lock.

    The bus also remembers the LAST compile result so a freshly
    connected browser tab can immediately see the current state (the
    overlay should pop up if the last compile failed).
    """

    def __init__(self):
        self._clients: List[HmrClient] = []
        self._lock = threading.Lock()
        self._next_id = 1
        # Last state for replay-on-connect.
        self._last_state: Optional[dict] = None  # {type, ...}
        self._on_log: Optional[Callable[[dict, HmrClient], None]] = None
        self._on_error: Optional[Callable[[dict, HmrClient], None]] = None
        self._on_identify: Optional[Callable[[dict, HmrClient], None]] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_callbacks(self,
                      on_log: Optional[Callable[[dict, HmrClient], None]] = None,
                      on_error: Optional[Callable[[dict, HmrClient], None]] = None,
                      on_identify: Optional[Callable[[dict, HmrClient], None]] = None
                      ) -> None:
        self._on_log = on_log
        self._on_error = on_error
        self._on_identify = on_identify

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def subscribe(self, sock, addr) -> HmrClient:
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            client = HmrClient(cid, sock, addr)
            self._clients.append(client)
            n = len(self._clients)
        # Send the hello frame (outside the lock — send_json has its own).
        client.send_json({
            "type": "hello",
            "version": HMR_PROTOCOL_VERSION,
            "server": SERVER_BANNER,
            "client_id": cid,
            "time": time.time(),
        })
        # Replay the last state so a new tab doesn't show stale UI.
        if self._last_state is not None:
            client.send_json(self._last_state)
        client.send_json({
            "type": "connected",
            "client_id": cid,
            "n_clients": n,
        })
        return client

    def unsubscribe(self, client: HmrClient) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        client.alive = False

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    def broadcast(self, obj: dict, *, remember: bool = False) -> None:
        """Send ``obj`` (a dict) to every connected client, in JSON.

        If ``remember`` is True, this message replaces the "last state"
        and is replayed to any client that connects later (used for
        ``ok`` and ``errors`` so a fresh tab reflects the current
        compile state immediately).
        """
        if remember:
            self._last_state = obj
        with self._lock:
            clients = list(self._clients)
        # Remove dead clients opportunistically.
        dead = []
        for c in clients:
            if not c.send_json(obj):
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)

    # Convenience typed senders --------------------------------------

    def send_ok(self, stats: dict, *, hot_reloadable: bool = True,
                wasm_url: Optional[str] = None) -> None:
        msg = {"type": "ok", "stats": stats,
               "hot_reloadable": hot_reloadable}
        if wasm_url:
            msg["wasm_url"] = wasm_url
        self.broadcast(msg, remember=True)

    def send_errors(self, errors_payload: dict) -> None:
        self.broadcast({"type": "errors", **errors_payload},
                       remember=True)

    def send_warnings(self, warnings: list) -> None:
        self.broadcast({"type": "warnings", "warnings": warnings})

    def send_progress(self, phase: str, percent: int,
                      detail: str = "") -> None:
        self.broadcast({"type": "progress", "phase": phase,
                        "percent": percent, "detail": detail})

    def send_reload(self, reason: str = "wasm-changed") -> None:
        self.broadcast({"type": "reload", "reason": reason})

    def send_hot_reload(self, wasm_url: str, reason: str = "") -> None:
        self.broadcast({"type": "hot-reload", "wasm_url": wasm_url,
                        "reason": reason})


# ---------------------------------------------------------------------------
# Per-client read loop
# ---------------------------------------------------------------------------

def serve_client_loop(bus: HmrBus, client: HmrClient) -> None:
    """Read frames from ``client.sock`` until the connection closes.

    Runs in its OWN thread (one per client — a typical dev session has
    1-3 clients, so the overhead is negligible). Each inbound message
    is dispatched to the appropriate callback.
    """
    try:
        while client.alive:
            frame = ws_read_frame(client.sock)
            if frame is None:
                break
            _fin, opcode, payload = frame
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                # Echo as pong.
                try:
                    client.sock.sendall(
                        ws_encode_frame(payload, opcode=OP_PONG))
                except OSError:
                    break
                continue
            if opcode == OP_PONG:
                continue
            if opcode not in (OP_TEXT, OP_BIN, OP_CONT):
                continue
            if not payload:
                continue
            # Decode JSON.
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception:
                # Malformed JSON; ignore.
                continue
            mtype = obj.get("type")
            if mtype == "ping":
                client.send_json({"type": "pong"})
                continue
            if mtype == "identify" and bus._on_identify:
                client.ident = obj
                try:
                    bus._on_identify(obj, client)
                except Exception as e:
                    debug("on_identify raised: %s" % e)
                continue
            if mtype == "log" and bus._on_log:
                try:
                    bus._on_log(obj, client)
                except Exception as e:
                    debug("on_log raised: %s" % e)
                continue
            if mtype == "error" and bus._on_error:
                try:
                    bus._on_error(obj, client)
                except Exception as e:
                    debug("on_error raised: %s" % e)
                continue
    except OSError:
        pass
    finally:
        bus.unsubscribe(client)
        try:
            client.sock.close()
        except OSError:
            pass
