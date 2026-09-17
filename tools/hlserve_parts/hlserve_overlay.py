"""hlserve_overlay — Stage 75 in-browser compile-error overlay.

The Stage 24 dev server injected a tiny SSE listener snippet. Stage 75
adds a full HMR client + an error overlay that:

* Pops up when a compile fails (red banner + modal with file:line +
  source context).
* Auto-dismisses when the next compile succeeds.
* Stays out of the way when there are no errors (no fixed banner).
* Shows build progress (compile-start, optimise-start, compile-end).
* Shows bundle stats (wasm size, compile time) in a small footer.
* Forwards ``console.log`` from the wasm program to the dev server's
  terminal (via the WS log channel).
* Reloads the page on ``reload`` events; hot-swaps the wasm module on
  ``hot-reload`` events (preserves JS state).

The overlay is a single self-contained <script> + <style> injected
into every HTML page served at ``/`` (just before ``</body>``). It
opens a WebSocket to ``/ws`` and dispatches messages from the bus.
"""
from __future__ import annotations

# The HMR client + overlay, all in one JS snippet. Designed to be:
#  * Self-contained (no external deps).
#  * Defensive (every WS message is type-checked before use).
#  * Idempotent (re-injected on every full reload, so the snippet
#    must tolerate being defined twice — guarded by a global flag).
#  * Quiet on browsers without WebSocket (very old, but the loader
#    degrades to "no overlay" rather than crashing the page).
OVERLAY_SNIPPET = r"""<script id="hlserve-hmr-client">
(function(){
  if (window.__HLSERVE_HMR__) return; // already loaded
  window.__HLSERVE_HMR__ = true;
  var PROTO_VERSION = 1;
  var wsUrl = (location.protocol === "https:" ? "wss://" : "ws://")
            + location.host + "/ws";
  var overlay = null;
  var bannerEl = null;
  var statsEl = null;
  var lastErrors = null;
  var client_id = null;

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "hlserve-overlay";
    overlay.style.cssText = [
      "position:fixed","top:0","left:0","right:0","bottom:0",
      "z-index:2147483647","background:rgba(40,0,0,0.92)",
      "color:#fdd","font-family:Menlo,Consolas,monospace",
      "font-size:14px","padding:24px 32px",
      "overflow:auto","display:none",
      "box-sizing:border-box","line-height:1.5"
    ].join(";");
    var h = document.createElement("div");
    h.style.cssText = "font-weight:bold;color:#fff;font-size:18px;"
                    + "margin-bottom:12px;border-bottom:1px solid #800;"
                    + "padding-bottom:8px;";
    h.textContent = "hls-serve — compile errors";
    overlay.appendChild(h);
    var close = document.createElement("button");
    close.textContent = "x";
    close.style.cssText = "position:fixed;top:24px;right:32px;"
        + "background:#400;color:#fdd;border:1px solid #800;"
        + "padding:4px 10px;cursor:pointer;font-size:14px;";
    close.onclick = function(){ overlay.style.display = "none"; };
    overlay.appendChild(close);
    var body = document.createElement("div");
    body.id = "hlserve-overlay-body";
    overlay.appendChild(body);
    document.body.appendChild(overlay);
    return overlay;
  }

  function ensureStatsBar() {
    if (statsEl) return statsEl;
    statsEl = document.createElement("div");
    statsEl.id = "hlserve-stats";
    statsEl.style.cssText = [
      "position:fixed","bottom:0","right:0","z-index:2147483646",
      "background:rgba(0,40,0,0.9)","color:#afa","font-family:Menlo,"
      + "Consolas,monospace","font-size:12px","padding:4px 10px",
      "border-top-left-radius:6px","max-width:50vw","overflow:hidden",
      "white-space:nowrap","text-overflow:ellipsis","pointer-events:none"
    ].join(";");
    statsEl.textContent = "hls-serve: connecting…";
    document.body.appendChild(statsEl);
    return statsEl;
  }

  function showOverlay(errors) {
    var o = ensureOverlay();
    var body = document.getElementById("hlserve-overlay-body");
    body.innerHTML = "";
    errors.forEach(function(e){
      var d = document.createElement("div");
      d.style.cssText = "margin-bottom:12px;padding:8px 0;"
                      + "border-bottom:1px dotted #800;";
      var sev = (e.severity || "error").toUpperCase();
      var sevColor = sev === "ERROR" ? "#f88" :
                     sev === "WARNING" ? "#fc8" : "#8cf";
      var head = document.createElement("div");
      head.innerHTML = "<span style=\"color:" + sevColor + ";"
        + "font-weight:bold;\">" + sev + "</span> "
        + "<span style=\"color:#fff;\">" + (e.file || "(no file)")
        + "</span>"
        + (e.line ? "<span style=\"color:#aaf;\">:" + e.line
                  + (e.col ? ":" + e.col : "") + "</span>" : "");
      d.appendChild(head);
      var msg = document.createElement("div");
      msg.style.cssText = "margin-left:16px;margin-top:4px;color:#fdd;";
      msg.textContent = e.message || "(no message)";
      d.appendChild(msg);
      body.appendChild(d);
    });
    o.style.display = "block";
  }

  function hideOverlay() {
    if (overlay) overlay.style.display = "none";
  }

  function setStats(text, color) {
    ensureStatsBar();
    statsEl.textContent = "hls-serve: " + text;
    statsEl.style.background = "rgba(" + (color || "0,40,0") + ",0.9)";
  }

  function applyErrors(payload) {
    lastErrors = payload;
    var errs = (payload && payload.errors) || [];
    var real = errs.filter(function(e){
      return (e.severity || "error") === "error";
    });
    if (real.length > 0) {
      showOverlay(real);
      setStats(real.length + " error(s)", "40,0,0");
    } else if (errs.length > 0) {
      setStats(errs.length + " warning(s)", "40,30,0");
    }
  }

  function applyOk(payload) {
    hideOverlay();
    var stats = payload.stats || {};
    var wasmKB = (stats.wasm_bytes || 0) / 1024;
    wasmKB = wasmKB.toFixed(1);
    var jsKB = (stats.js_bytes || 0) / 1024;
    jsKB = jsKB.toFixed(1);
    setStats("ok | wasm " + wasmKB + " KB | js " + jsKB + " KB | "
             + (stats.elapsed_ms || 0) + " ms");
  }

  function hotReload(wasmUrl) {
    // Re-fetch the wasm module and re-instantiate the existing Halis
    // instance. Preserves all JS-side state.
    if (typeof Halis === "undefined" || !Halis.__instance) {
      // Can't hot-swap; fall back to full reload.
      location.reload();
      return;
    }
    fetch(wasmUrl, {cache: "no-store"})
      .then(function(r){ return r.arrayBuffer(); })
      .then(function(buf){
        var bytes = new Uint8Array(buf);
        if (Halis.hotReload) {
          return Halis.hotReload(bytes);
        }
        // Fall back: full re-instantiation with the same imports.
        return Halis.__instance.instantiate(bytes, Halis.__imports || {});
      })
      .then(function(){
        setStats("hot-reloaded", "0,40,0");
      })
      .catch(function(e){
        // Hot reload failed; fall back to full page reload.
        setStats("hot-reload failed: " + e.message, "40,0,0");
        setTimeout(function(){ location.reload(); }, 500);
      });
  }

  // Console forwarder: hook console.log/warn/error and forward to WS.
  function forwardConsole(method, ws_level) {
    var orig = console[method] ? console[method].bind(console) : null;
    console[method] = function(){
      try {
        if (ws && ws.readyState === 1) {
          var args = Array.prototype.slice.call(arguments).map(function(a){
            try { return typeof a === "string" ? a : JSON.stringify(a); }
            catch(_) { return String(a); }
          });
          ws.send(JSON.stringify({type:"log", level:ws_level, args:args}));
        }
      } catch(_) {}
      if (orig) orig.apply(console, arguments);
    };
  }

  var ws = null;
  function connect() {
    try { ws = new WebSocket(wsUrl); }
    catch(e) { setTimeout(connect, 2000); return; }
    ws.onopen = function(){
      ensureStatsBar();
      setStats("connected");
      try {
        ws.send(JSON.stringify({type:"identify", client:"browser",
                                id: Math.random().toString(36).slice(2)}));
      } catch(_) {}
    };
    ws.onmessage = function(ev){
      var msg;
      try { msg = JSON.parse(ev.data); } catch(_) { return; }
      if (msg.version && msg.version !== PROTO_VERSION) {
        setStats("proto mismatch (server v" + msg.version + ")", "40,0,0");
        return;
      }
      switch (msg.type) {
        case "hello":
          client_id = msg.client_id;
          break;
        case "connected":
          break;
        case "ok":
          applyOk(msg);
          break;
        case "errors":
          applyErrors(msg);
          break;
        case "warnings":
          break;
        case "progress":
          if (msg.phase && msg.percent != null) {
            setStats(msg.phase + " " + msg.percent + "%", "0,30,40");
          }
          break;
        case "reload":
          // Full reload: server-side reload (HTML change, glue change).
          location.reload();
          break;
        case "hot-reload":
          if (msg.wasm_url) hotReload(msg.wasm_url);
          break;
        case "pong":
          break;
      }
    };
    ws.onclose = function(){
      setStats("disconnected — retrying", "40,30,0");
      setTimeout(connect, 1000);
    };
    ws.onerror = function(){
      try { ws.close(); } catch(_) {}
    };
  }

  forwardConsole("log", "log");
  forwardConsole("warn", "warn");
  forwardConsole("error", "error");

  // Catch uncaught client errors.
  window.addEventListener("error", function(ev){
    if (ws && ws.readyState === 1 && ev.message) {
      try {
        ws.send(JSON.stringify({type:"error", message: ev.message,
                                stack: ev.error && ev.error.stack
                                  ? ev.error.stack : ""}));
      } catch(_) {}
    }
  });

  if (typeof WebSocket !== "undefined") {
    // Wait for DOMContentLoaded so document.body exists.
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", connect);
    } else {
      connect();
    }
  }
})();
</script>
"""


def inject_overlay(html_bytes: bytes, overlay_enabled: bool = True) -> bytes:
    """Inject the HMR overlay snippet into ``html_bytes``.

    The snippet is placed just before ``</body>`` (so it runs after the
    page's own scripts have set up their state — important for
    hot-reload to find the existing ``Halis.__instance``). If there's
    no ``</body>``, the snippet is appended.

    When ``overlay_enabled`` is False (the user passed ``--no-overlay``),
    the snippet is NOT injected — useful for production builds or
    headless browser tests.
    """
    if not overlay_enabled:
        return html_bytes
    snippet = OVERLAY_SNIPPET.encode("utf-8")
    if b"</body>" in html_bytes:
        return html_bytes.replace(b"</body>", snippet + b"</body>", 1)
    return html_bytes + snippet


def inject_status_banner(html_bytes: bytes, message: str,
                          color: str = "#800") -> bytes:
    """Inject a compile-failed banner at the top of ``<body>``. Used
    for the case where compile failed BEFORE the HMR WS is even up —
    the overlay won't connect (no JS yet), so a static banner is the
    only feedback the user gets."""
    if not message:
        return html_bytes
    banner = (
        b'<div style="background:#fee;color:' + color.encode("ascii") + b';'
        b'border-bottom:1px solid ' + color.encode("ascii") + b';'
        b'padding:0.5rem;font-family:Menlo,Consolas,monospace;'
        b'font-size:14px;">[hls-serve] ' + message.encode("utf-8") + b'</div>'
    )
    if b"<body" in html_bytes:
        idx = html_bytes.find(b">", html_bytes.find(b"<body")) + 1
        return html_bytes[:idx] + banner + html_bytes[idx:]
    return banner + html_bytes
