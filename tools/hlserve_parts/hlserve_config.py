"""hlserve_config — Stage 75 ``hls.serve.toml`` reader.

The Stage 24 dev server took every option from the command line. Stage
75 adds a config-file layer so a Halis web app can drop a
``hls.serve.toml`` next to ``hls.serve.json`` into the project root
and not repeat the flags on every ``make serve`` / ``hls serve`` call.

Sample ``hls.serve.toml``::

    # hls.serve.toml — Stage 75 dev-server config
    input = "examples/web_app_1000loc.hls"
    port = 3000
    open = true
    https = true
    history_fallback = true
    compress = true

    [watch]
    dirs = ["src", "std", "examples"]
    debounce_ms = 200

    [[proxy]]
    path = "/api"
    target = "http://localhost:3001"

    [[proxy]]
    path = "/ws-backend"
    target = "ws://localhost:3002"

A ``hls.serve.json`` (JSON, same shape, camelCase keys) is also accepted
for editors that don't have a TOML plugin. If both exist, TOML wins.

The CLI ``--port`` etc. always override the config file (the Stage 24
CLI surface is preserved exactly).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProxyRule:
    """One ``--proxy /api=http://localhost:3001`` rule.

    ``prefix`` is the URL path prefix (e.g. ``/api``). ``target`` is the
    upstream base URL (e.g. ``http://localhost:3001``). A request to
    ``/api/users`` is forwarded to ``http://localhost:3001/api/users``
    (the prefix is preserved — mirroring webpack-dev-server's default
    ``pathRewrite: {}`` semantics; if you want the prefix stripped,
    that's a future ``pathRewrite`` option).
    """
    prefix: str
    target: str

    def __post_init__(self):
        if not self.prefix.startswith("/"):
            self.prefix = "/" + self.prefix
        # Normalise: no trailing slash on prefix (so "/api" matches both
        # "/api" and "/api/users").
        self.prefix = self.prefix.rstrip("/")
        if not self.target.startswith(("http://", "https://",
                                        "ws://", "wss://")):
            raise ValueError("proxy target must include the scheme "
                             "(http://, https://, ws://, wss://): %r"
                             % self.target)


@dataclass
class ServeConfig:
    """Full dev-server config. Defaults mirror the Stage 24 CLI."""

    # Top-level
    input: str = "examples/hello.hls"
    bundle: str = "out"
    port: int = 3000
    host: str = "0.0.0.0"
    target: str = "wasm32-unknown-unknown"
    wasm_opt: str = "auto"
    opt_level: str = "O3"
    glue: str = "compact"

    # New Stage 75 options
    open: bool = False
    https: bool = False
    history_fallback: bool = True
    compress: bool = True
    public_dir: Optional[str] = None
    color: Optional[bool] = None  # None = auto-detect TTY

    # Watch options
    watch_dirs: List[str] = field(default_factory=list)
    watch_extra: List[str] = field(default_factory=list)
    debounce_ms: int = 200

    # Proxy rules
    proxies: List[ProxyRule] = field(default_factory=list)

    # Logging
    verbose: bool = False
    quiet: bool = False

    # Source map / hot reload behaviour
    hot_reload: bool = True    # wasm module swap (vs full page reload)
    overlay: bool = True       # show compile errors in an overlay

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # ProxyRule -> dict already handled by asdict; flatten nothing else.
        return d


# ---------------------------------------------------------------------------
# CLI-spec parsers (``--proxy /api=http://localhost:3001``)
# ---------------------------------------------------------------------------

def parse_proxy_spec(spec: str) -> ProxyRule:
    """Parse ``"prefix=target"`` (e.g. ``"/api=http://localhost:3001"``).

    The ``=`` is the separator (chosen because the URL also contains
    ``:`` and ``/``). Both halves are required.
    """
    if "=" not in spec:
        raise ValueError("proxy spec must be PREFIX=TARGET (e.g. "
                         "/api=http://localhost:3001); got: %r" % spec)
    prefix, target = spec.split("=", 1)
    prefix = prefix.strip()
    target = target.strip()
    if not prefix or not target:
        raise ValueError("proxy spec prefix and target must both be "
                         "non-empty; got: %r" % spec)
    return ProxyRule(prefix=prefix, target=target)


def parse_proxy_specs(specs: List[str]) -> List[ProxyRule]:
    """Parse multiple specs (one per ``--proxy`` flag)."""
    return [parse_proxy_spec(s) for s in specs]


# ---------------------------------------------------------------------------
# Config-file loading
# ---------------------------------------------------------------------------

_TOML_PATHS = ("hls.serve.toml", "hls.serve.json", ".hls.serve.toml")


def find_config_file(cwd: Optional[str] = None) -> Optional[str]:
    """Search ``cwd`` (default: the process cwd) for the first matching
    config file. Returns the absolute path, or None when no file exists."""
    if cwd is None:
        cwd = os.getcwd()
    for name in _TOML_PATHS:
        p = os.path.join(cwd, name)
        if os.path.isfile(p):
            return p
    return None


def _try_tomllib(text: str) -> Optional[Dict[str, Any]]:
    """Try to parse ``text`` as TOML. Returns None on failure.

    Uses stdlib ``tomllib`` (Python 3.11+). Older Pythons get None and
    the caller falls back to JSON parsing of the same file content (so
    a user who only has 3.10 can still use a ``.json`` file).
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return tomllib.loads(text)
    except Exception:
        return None


def load_config_file(path: str) -> Dict[str, Any]:
    """Load a config file (TOML or JSON) into a dict.

    The file extension decides the parser. ``.toml`` is parsed with
    ``tomllib`` when available; if ``tomllib`` is missing and the file
    is also valid JSON, we accept that (some users write TOML files
    that are also JSON-valid — rare but defensive). ``.json`` always
    uses JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        return json.loads(text)
    # TOML: prefer tomllib; fall back to JSON for graceful degradation.
    parsed = _try_tomllib(text)
    if parsed is not None:
        return parsed
    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError("config file %r is neither valid TOML nor "
                         "valid JSON: %s" % (path, e))


def config_dict_to_serve_config(d: Dict[str, Any]) -> ServeConfig:
    """Translate the parsed dict into a ``ServeConfig``.

    The TOML/JSON shape is mostly the camelCase / snake_case mirror of
    the dataclass fields, with these differences:

    * ``[[proxy]]`` array of ``{path = ..., target = ...}`` (TOML) or
      ``"proxy": [{"path": ..., "target": ...}]`` (JSON) -> ``proxies``
      list of ``ProxyRule``.
    * ``[watch]`` table with ``dirs`` and ``debounce_ms`` ->
      ``watch_dirs`` + ``debounce_ms``.
    """
    cfg = ServeConfig()
    # Top-level scalar fields (snake_case in both TOML and JSON).
    for fld in ("input", "bundle", "target", "wasm_opt", "opt_level",
                "glue", "host", "color"):
        if fld in d:
            setattr(cfg, fld, d[fld])
    # Integer port
    if "port" in d:
        cfg.port = int(d["port"])
    # Boolean flags
    for fld in ("open", "https", "history_fallback", "compress",
                "hot_reload", "overlay", "verbose", "quiet"):
        if fld in d:
            setattr(cfg, fld, bool(d[fld]))
    # Optional fields (None default)
    if "public_dir" in d:
        cfg.public_dir = d["public_dir"] or None
    # Watch table
    w = d.get("watch", {}) or {}
    if isinstance(w, dict):
        if "dirs" in w and isinstance(w["dirs"], list):
            cfg.watch_dirs = [str(x) for x in w["dirs"]]
        if "extra" in w and isinstance(w["extra"], list):
            cfg.watch_extra = [str(x) for x in w["extra"]]
        if "debounce_ms" in w:
            cfg.debounce_ms = int(w["debounce_ms"])
    # Proxy array
    px = d.get("proxy", []) or []
    if isinstance(px, list):
        cfg.proxies = []
        for entry in px:
            if not isinstance(entry, dict):
                continue
            p = entry.get("path") or entry.get("prefix")
            t = entry.get("target")
            if p and t:
                cfg.proxies.append(ProxyRule(prefix=str(p), target=str(t)))
    return cfg


# ---------------------------------------------------------------------------
# CLI args -> ServeConfig merge
# ---------------------------------------------------------------------------

def merge_args(args, cfg: ServeConfig) -> ServeConfig:
    """Return a NEW ``ServeConfig`` that starts from ``cfg`` and is
    overridden by any non-None CLI arg in ``args`` (an
    ``argparse.Namespace``).

    The rule is "CLI wins": a flag that was NOT explicitly given on the
    command line keeps the config-file value (which itself defaults to
    the dataclass defaults if no config file exists).
    """
    out = ServeConfig(
        input=cfg.input,
        bundle=cfg.bundle,
        port=cfg.port,
        host=cfg.host,
        target=cfg.target,
        wasm_opt=cfg.wasm_opt,
        opt_level=cfg.opt_level,
        glue=cfg.glue,
        open=cfg.open,
        https=cfg.https,
        history_fallback=cfg.history_fallback,
        compress=cfg.compress,
        public_dir=cfg.public_dir,
        color=cfg.color,
        watch_dirs=list(cfg.watch_dirs),
        watch_extra=list(cfg.watch_extra),
        debounce_ms=cfg.debounce_ms,
        proxies=list(cfg.proxies),
        verbose=cfg.verbose,
        quiet=cfg.quiet,
        hot_reload=cfg.hot_reload,
        overlay=cfg.overlay,
    )
    # argparse default values are used as sentinels: any value that
    # matches the parser default means "user didn't pass this flag".
    # The parser uses the SAME defaults as the dataclass, so equality
    # means "untouched".
    if getattr(args, "input", None) and args.input != "examples/hello.hls":
        out.input = args.input
    if getattr(args, "bundle", None) and args.bundle != "out":
        out.bundle = args.bundle
    if getattr(args, "port", None) and args.port != 3000:
        out.port = int(args.port)
    if getattr(args, "target", None) and args.target != "wasm32-unknown-unknown":
        out.target = args.target
    if getattr(args, "wasm_opt", None) and args.wasm_opt != "auto":
        out.wasm_opt = args.wasm_opt
    if getattr(args, "glue", None) and args.glue != "compact":
        out.glue = args.glue
    # Booleans: --open / --https / --history-fallback / --compress /
    # --verbose / --quiet use store_true/store_false with default False;
    # the user "touched" them when True. The ``--no-*`` counterparts
    # set them to False explicitly via the parser.
    if getattr(args, "open", False):
        out.open = True
    if getattr(args, "https", False):
        out.https = True
    if getattr(args, "history_fallback_off", False):
        out.history_fallback = False
    elif getattr(args, "history_fallback", False):
        out.history_fallback = True
    if getattr(args, "compress_off", False):
        out.compress = False
    elif getattr(args, "compress", False):
        out.compress = True
    if getattr(args, "verbose", False):
        out.verbose = True
    if getattr(args, "quiet", False):
        out.quiet = True
    if getattr(args, "hot_reload_off", False):
        out.hot_reload = False
    if getattr(args, "overlay_off", False):
        out.overlay = False
    # Public dir.
    if getattr(args, "public_dir", None):
        out.public_dir = args.public_dir
    # Color flags.
    if getattr(args, "color", False):
        out.color = True
    elif getattr(args, "no_color", False):
        out.color = False
    # Watch dirs and extra watch dirs.
    if getattr(args, "watch", None):
        # Single --watch DIR (Stage 24 compat).
        out.watch_extra.append(os.path.abspath(args.watch))
    if getattr(args, "watch_dirs", None):
        for d in args.watch_dirs:
            out.watch_extra.append(os.path.abspath(d))
    # Debounce.
    if getattr(args, "debounce_ms", None) and args.debounce_ms != 200:
        out.debounce_ms = int(args.debounce_ms)
    # Proxies.
    if getattr(args, "proxy", None):
        out.proxies.extend(parse_proxy_specs(args.proxy))
    # Listen address.
    if getattr(args, "listen", None):
        from hlserve_common import parse_listen_addr
        host, port = parse_listen_addr(args.listen, out.port)
        out.host = host
        out.port = port
    return out


def default_config_for_cwd(cwd: Optional[str] = None) -> ServeConfig:
    """Build a ``ServeConfig`` from the config file (if any) in ``cwd``,
    else the dataclass defaults."""
    path = find_config_file(cwd)
    if path is None:
        return ServeConfig()
    try:
        d = load_config_file(path)
        return config_dict_to_serve_config(d)
    except Exception as e:
        from hlserve_common import warn
        warn("could not parse config file %s: %s; using defaults"
             % (path, e))
        return ServeConfig()
