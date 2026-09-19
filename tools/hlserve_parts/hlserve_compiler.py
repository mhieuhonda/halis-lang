"""hlserve_compiler — Stage 75 compile wrapper around ``hlwasm``.

Stage 24's ``compile_bundle`` was a thin wrapper that returned ``True``/
``False``. Stage 75 needs richer information for:

* The error overlay (file:line + message + severity).
* The build-progress UI (compile-start, optimise-start, compile-end).
* The bundle-stats message (wasm size, js size, compile time).

So this module wraps ``hlwasm.compile_program`` and parses its stderr
output into structured ``CompileError`` records, plus measures
``elapsed_ms`` with ``time.perf_counter``.

The compiler errors emitted by ``hlc`` (and the Stage-0 interpreter
used inside ``hlwasm.compile_program``) follow the form::

    <file>:<line>:<col>: <severity>: <message>

or, for older emits::

    <file>:<line>: error: <message>

We parse both, plus the generic Python traceback form
``<file>:<line>: <message>`` (the Python seed emits these when the
checker panics — defensive).
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import List, Optional


# Regex for ``path:line:col: SEVERITY: message``
_ERROR_RE_LC_SEV = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<sev>error|warning|note|help):\s*(?P<msg>.*)$",
    re.IGNORECASE)
# Regex for ``path:line: SEVERITY: message`` (no column — older emits)
_ERROR_RE_L_SEV = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\s*"
    r"(?P<sev>error|warning|note|help):\s*(?P<msg>.*)$",
    re.IGNORECASE)
# Regex for ``path:line: message`` (no severity, default to error)
_ERROR_RE_LC = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\s*(?P<msg>.*)$")
# Regex for ``path:line:col: message`` (no severity)
_ERROR_RE_LC_COL = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.*)$")


@dataclass
class CompileError:
    """One structured compiler diagnostic.

    ``severity`` is one of ``"error"``, ``"warning"``, ``"note"``,
    ``"help"``. ``file`` is the path as emitted by the compiler
    (relative to the repo root, usually). ``line``/``col`` are 1-based;
    ``0`` when the compiler didn't emit a position.
    """
    severity: str
    file: str
    line: int
    col: int
    message: str

    def to_overlay_dict(self) -> dict:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "message": self.message,
        }

    def format_for_terminal(self) -> str:
        if self.line and self.col:
            return "%s:%d:%d: %s: %s" % (self.file, self.line, self.col,
                                          self.severity, self.message)
        if self.line:
            return "%s:%d: %s: %s" % (self.file, self.line,
                                       self.severity, self.message)
        return "%s: %s" % (self.file, self.message)


@dataclass
class CompileResult:
    """The full outcome of one ``compile_once`` invocation.

    ``ok`` is True iff the wasm bundle was written successfully.
    ``errors`` are the parsed diagnostics (empty if ok). ``stderr`` is
    the raw stderr stream for the log file.
    """
    ok: bool
    elapsed_ms: int
    wasm_path: Optional[str] = None
    js_path: Optional[str] = None
    html_path: Optional[str] = None
    wasm_bytes: int = 0
    js_bytes: int = 0
    errors: List[CompileError] = field(default_factory=list)
    stderr: str = ""
    stdout: str = ""
    return_code: int = 0

    def to_stats_dict(self) -> dict:
        """Payload for the HMR ``ok`` / ``stats`` message."""
        return {
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "wasm_bytes": self.wasm_bytes,
            "js_bytes": self.js_bytes,
            "wasm_url": (os.path.basename(self.wasm_path)
                         if self.wasm_path else None),
            "js_url": (os.path.basename(self.js_path)
                       if self.js_path else None),
            "error_count": sum(1 for e in self.errors
                               if e.severity == "error"),
            "warning_count": sum(1 for e in self.errors
                                 if e.severity == "warning"),
        }

    def to_errors_dict(self) -> dict:
        """Payload for the HMR ``errors`` message."""
        return {
            "errors": [e.to_overlay_dict() for e in self.errors],
            "elapsed_ms": self.elapsed_ms,
        }


def parse_diagnostics(stderr_text: str) -> List[CompileError]:
    """Parse compiler stderr into structured diagnostics.

    Each line is tried against three regexes in order. Lines that don't
    match any are silently dropped (the compiler's free-form progress
    lines like ``"wrote out.wasm (1234 bytes)"`` shouldn't pollute the
    overlay).
    """
    diags: List[CompileError] = []
    for line in stderr_text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        m = _ERROR_RE_LC_SEV.match(line)
        if m:
            diags.append(CompileError(
                severity=m.group("sev").lower(),
                file=m.group("file"),
                line=int(m.group("line") or 0),
                col=int(m.group("col") or 0),
                message=m.group("msg"),
            ))
            continue
        m = _ERROR_RE_L_SEV.match(line)
        if m:
            diags.append(CompileError(
                severity=m.group("sev").lower(),
                file=m.group("file"),
                line=int(m.group("line") or 0),
                col=0,
                message=m.group("msg"),
            ))
            continue
        m = _ERROR_RE_LC_COL.match(line)
        if m:
            diags.append(CompileError(
                severity="error",
                file=m.group("file"),
                line=int(m.group("line") or 0),
                col=int(m.group("col") or 0),
                message=m.group("msg"),
            ))
            continue
        m = _ERROR_RE_LC.match(line)
        if m:
            diags.append(CompileError(
                severity="error",
                file=m.group("file"),
                line=int(m.group("line") or 0),
                col=0,
                message=m.group("msg"),
            ))
            continue
    return diags


def compile_once(input_hls: str,
                 output_base: str,
                 target: str = "wasm32-unknown-unknown",
                 wasm_opt: str = "auto",
                 opt_level: str = "O3",
                 glue: str = "compact",
                 emit_html: bool = True) -> CompileResult:
    """Compile ``input_hls`` to ``output_base.{wasm,js,html}`` using
    the in-tree ``hlwasm.compile_program``. Returns a ``CompileResult``
    capturing timing, parsed diagnostics, and the output paths.

    Errors from hlwasm are captured (not propagated) — the caller can
    decide whether to surface them via the HMR bus or to crash. The
    dev server keeps running on a failed compile (the user fixes the
    code, saves, and the overlay auto-dismisses).
    """
    start = time.perf_counter()
    err_buf = io.StringIO()
    out_buf = io.StringIO()
    rc = 0
    try:
        # Import hlwasm from the tools directory.
        tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from hlwasm import compile_program  # type: ignore
        with redirect_stderr(err_buf), redirect_stdout(out_buf):
            rc = compile_program(
                input_hls, output_base,
                target=target,
                wasm_opt=wasm_opt,
                opt_level=opt_level,
                glue_style=glue,
                emit_html=emit_html,
            )
    except ImportError as e:
        return CompileResult(
            ok=False,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
            errors=[CompileError(
                severity="error",
                file="<hlserve>",
                line=0, col=0,
                message="cannot import hlwasm: %s" % e)],
            stderr=err_buf.getvalue(),
            stdout=out_buf.getvalue(),
            return_code=1,
        )
    except Exception as e:
        # HLError, panics, anything.
        return CompileResult(
            ok=False,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
            errors=[CompileError(
                severity="error",
                file=input_hls,
                line=0, col=0,
                message="compile raised: %s: %s" % (type(e).__name__, e))],
            stderr=err_buf.getvalue() + "\n" + repr(e),
            stdout=out_buf.getvalue(),
            return_code=2,
        )
    stderr_text = err_buf.getvalue()
    stdout_text = out_buf.getvalue()
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    diags = parse_diagnostics(stderr_text)
    # Augment: if hlwasm returned nonzero but we parsed no diagnostics,
    # promote the first non-empty stderr line into a synthetic error
    # (so the overlay always has something to show).
    if rc != 0 and not any(d.severity == "error" for d in diags):
        for line in stderr_text.splitlines():
            line = line.strip()
            if line and not line.startswith("hlwasm:") and \
                    not line.startswith("wrote ") and \
                    not line.startswith("note:") and \
                    not line.startswith("warning:"):
                diags.append(CompileError(
                    severity="error",
                    file=input_hls,
                    line=0, col=0,
                    message=line,
                ))
                break
        if not diags:
            diags.append(CompileError(
                severity="error",
                file=input_hls,
                line=0, col=0,
                message="compile failed (rc=%d); see stderr" % rc,
            ))
    # Output paths.
    wasm_path = output_base + ".wasm"
    js_path = output_base + ".js"
    html_path = output_base + ".html"
    wasm_bytes = (os.path.getsize(wasm_path)
                  if os.path.isfile(wasm_path) else 0)
    js_bytes = (os.path.getsize(js_path)
                if os.path.isfile(js_path) else 0)
    ok = (rc == 0) and wasm_bytes > 0
    return CompileResult(
        ok=ok,
        elapsed_ms=elapsed_ms,
        wasm_path=wasm_path if os.path.isfile(wasm_path) else None,
        js_path=js_path if os.path.isfile(js_path) else None,
        html_path=html_path if os.path.isfile(html_path) else None,
        wasm_bytes=wasm_bytes,
        js_bytes=js_bytes,
        errors=diags,
        stderr=stderr_text,
        stdout=stdout_text,
        return_code=rc,
    )
