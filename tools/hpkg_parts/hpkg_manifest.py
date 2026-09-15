"""manifest - verbatim segment of the original tools/hls-pkg.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hpkg_common import (
    Dict, Optional, Tuple, os,
)

def parse_manifest(path: str) -> Dict:
    """Parse a hls-pkg.toml manifest. Returns a dict.

    Supports the subset we need: section headers like [package],
    [dependencies], [effects], and `key = value` lines where value is
    a string, integer, list, or inline table { git = "...", path = "..." }.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError("manifest not found: %s" % path)
    with open(path, "r") as f:
        src = f.read()
    # Strip comments — only OUTSIDE strings.
    # BUG (deep-scan-5): the old code stripped everything after the first
    # `#` on a line regardless of quotes, so `name = "demo # 1"` silently
    # corrupted the manifest (the string parser then swallowed subsequent
    # lines looking for a closing quote, garbling every following key).
    lines = []
    for line in src.split("\n"):
        try:
            lines.append(_strip_toml_comment(line))
        except ValueError as ex:
            raise ValueError("manifest: %s (in line: %r)" % (ex, line)) from ex
    src = "\n".join(lines)

    # Parse into a tree of section -> key -> value.
    result: Dict = {}
    current_section: Optional[str] = None
    i = 0
    n = len(src)
    while i < n:
        # Skip whitespace.
        while i < n and src[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        # Section header.
        if src[i] == "[":
            j = src.index("]", i)
            current_section = src[i + 1:j].strip()
            if not current_section:
                # SCAN-B fix: empty section name `[]` creates `result[""]`
                # entries; round-trip emits invalid TOML.
                raise ValueError("manifest: empty section header at offset %d" % i)
            # Make sure the section dict exists.
            parts = current_section.split(".")
            cursor = result
            for p in parts:
                if not isinstance(cursor, dict):
                    raise ValueError("manifest: section [%s] shadows a non-table "
                                     "value (a key under [%s] was already set)"
                                     % (current_section, current_section))
                cursor = cursor.setdefault(p, {})
                if not isinstance(cursor, dict):
                    raise ValueError("manifest: section [%s] shadows a non-table "
                                     "value" % current_section)
            i = j + 1
            continue
        # key = value
        eq = src.index("=", i)
        key = src[i:eq].strip()
        if not key:
            # SCAN-B fix: `= value` (no key) silently created `result[""]`.
            raise ValueError("manifest: empty key at offset %d" % i)
        # Parse value.
        val, i = _parse_value(src, eq + 1)
        # Assign into the current section.
        if current_section is None:
            result[key] = val
        else:
            parts = current_section.split(".")
            cursor = result
            for p in parts:
                if not isinstance(cursor, dict):
                    raise ValueError("manifest: cannot set key under non-table "
                                     "section [%s]" % current_section)
                cursor = cursor.setdefault(p, {})
            if not isinstance(cursor, dict):
                raise ValueError("manifest: cannot set key under non-table "
                                 "section [%s]" % current_section)
            cursor[key] = val
    return result


def _strip_toml_comment(line: str) -> str:
    """Strip a `#` comment, but only OUTSIDE double-quoted strings.

    Deep-scan-12 fix (DSS-T-20): TOML strings only allow specific
    escape sequences (\\", \\\\, \\b, \\t, \\n, \\f, \\r, \\uXXXX,
    \\UXXXXXXXX). The previous code passed ANY escape through, so
    `name = "foo\\#bar"` was silently accepted as `foo#bar`. A strict
    TOML parser would reject the invalid escape. We now reject
    invalid escapes (raise ValueError) — the manifest parser wraps
    this in a clean error message."""
    out = []
    in_str = False
    i = 0
    n = len(line)
    # The valid TOML string escapes (TOML 1.0 §4.2).
    _TOML_ESCAPES = set('btnfr"\\')
    while i < n:
        c = line[i]
        if in_str:
            if c == '\\' and i + 1 < n:
                esc = line[i + 1]
                # TOML also allows \uXXXX and \UXXXXXXXX.
                if esc in _TOML_ESCAPES or esc in ("u", "U"):
                    out.append(c)
                    out.append(line[i + 1])
                    i += 2
                    continue
                raise ValueError(
                    "invalid TOML escape sequence: \\%s (TOML only allows "
                    "\\b, \\t, \\n, \\f, \\r, \\\", \\uXXXX, "
                    "\\UXXXXXXXX)" % esc)
            if c == '"':
                in_str = False
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == '#':
            break
        out.append(c)
        i += 1
    return "".join(out)


def _parse_value(src: str, i: int) -> Tuple[object, int]:
    """Parse a TOML value starting at position i. Returns (value, new_i)."""
    n = len(src)
    # Skip whitespace.
    while i < n and src[i] in " \t\r\n":
        i += 1
    if i >= n:
        return None, i
    c = src[i]
    if c == '"':
        # String (with escape decoding — BUG deep-scan-5: escapes were
        # never decoded, so `say \"hi\"` round-tripped with literal
        # backslashes and write_manifest grew them on every pass).
        j = i + 1
        out = []
        while j < n and src[j] != '"':
            if src[j] == '\\' and j + 1 < n:
                nxt = src[j + 1]
                if nxt == 'n':
                    out.append('\n')
                    j += 2
                elif nxt == 't':
                    out.append('\t')
                    j += 2
                elif nxt == 'r':
                    out.append('\r')
                    j += 2
                elif nxt == 'b':
                    # Deep-scan-20 fix (MEDIUM, TOML compliance): the
                    # previous code only decoded \n \t \r and passed
                    # everything else through as a literal char. \b
                    # became 'b' instead of backspace, \f became 'f'
                    # instead of form-feed, \uXXXX became 'uXXXX'.
                    out.append('\b')
                    j += 2
                elif nxt == 'f':
                    out.append('\f')
                    j += 2
                elif nxt == '"':
                    out.append('"')
                    j += 2
                elif nxt == '\\':
                    out.append('\\')
                    j += 2
                elif nxt == 'u' and j + 6 <= n:
                    # \uXXXX — 4 hex digits
                    try:
                        cp = int(src[j + 2:j + 6], 16)
                        out.append(chr(cp))
                    except ValueError:
                        raise ValueError("invalid \\u escape at offset %d" % j) from None
                    j += 6
                elif nxt == 'U' and j + 10 <= n:
                    # \UXXXXXXXX — 8 hex digits
                    try:
                        cp = int(src[j + 2:j + 10], 16)
                        out.append(chr(cp))
                    except ValueError:
                        raise ValueError("invalid \\U escape at offset %d" % j) from None
                    j += 10
                else:
                    # Unknown escape — preserve the char after the backslash.
                    out.append(nxt)
                    j += 2
            else:
                out.append(src[j])
                j += 1
        # Deep-scan-19 fix (MEDIUM): reject unterminated string literals.
        # The loop above exits at EOF without a closing quote; without
        # this guard, the partial content silently becomes the manifest
        # value (truncated, no error) — confusing package-resolution
        # failures downstream.
        if j >= n:
            raise ValueError("unterminated string literal at offset %d" % i)
        return "".join(out), j + 1
    if c == '[':
        # List of strings / bare tokens.
        # BUG (deep-scan-5): bare items (IO, Fs, integers) were consumed
        # char-by-char and silently DROPPED — `allowed = [IO, Fs]` parsed
        # to an empty list. Collect them with type conversion, mirroring
        # the bare-token branch below.
        j = i + 1
        items = []
        while j < n and src[j] != ']':
            while j < n and src[j] in " \t\r\n,":
                j += 1
            if j >= n or src[j] == ']':
                break
            if src[j] == '"':
                k = j + 1
                out = []
                while k < n and src[k] != '"':
                    if src[k] == '\\' and k + 1 < n:
                        nxt = src[k + 1]
                        if nxt == 'n':
                            out.append('\n')
                        elif nxt == 't':
                            out.append('\t')
                        elif nxt == 'r':
                            out.append('\r')
                        else:
                            out.append(nxt)
                        k += 2
                    else:
                        out.append(src[k])
                        k += 1
                items.append("".join(out))
                j = k + 1
            else:
                k = j
                while k < n and src[k] not in " \t\r\n,]":
                    k += 1
                tok = src[j:k]
                if tok:
                    if tok == "true":
                        items.append(True)
                    elif tok == "false":
                        items.append(False)
                    else:
                        try:
                            items.append(int(tok))
                            j = k
                            continue
                        except ValueError:
                            pass
                        try:
                            items.append(float(tok))
                            j = k
                            continue
                        except ValueError:
                            pass
                        items.append(tok)
                j = k
        return items, j + 1
    if c == '{':
        # Inline table.
        j = i + 1
        table = {}
        while j < n and src[j] != '}':
            while j < n and src[j] in " \t\r\n,":
                j += 1
            if j >= n or src[j] == '}':
                break
            eq = src.index("=", j)
            key = src[j:eq].strip()
            val, j = _parse_value(src, eq + 1)
            table[key] = val
        return table, j + 1
    # Bare token: integer, true/false, or unquoted string.
    # Deep-scan-20 fix (LOW, malformed input): the terminator set was
    # missing `]`, `}`, `=`, and `#`. A malformed manifest like
    # `{ key = foo}bar }` consumed `foo}bar` as a single token. Now
    # the parser stops at any structural character.
    j = i
    while j < n and src[j] not in " \t\r\n,]}=#":
        j += 1
    tok = src[i:j]
    if tok == "true":
        return True, j
    if tok == "false":
        return False, j
    try:
        return int(tok), j
    except ValueError:
        pass
    try:
        return float(tok), j
    except ValueError:
        pass
    return tok, j


def write_manifest(manifest: Dict, path: str):
    """Write a manifest dict as TOML.

    BUG (deep-scan-5): previously only [package]/[dependencies]/[effects]
    were written — `hls-pkg add` silently DELETED any other section the
    user had (e.g. [features]). Round-trip unknown sections too.
    """
    known = ["package", "dependencies", "effects"]
    lines = []
    for section in known:
        if section in manifest:
            lines.append("[%s]" % section)
            for k, v in manifest[section].items():
                lines.append('%s = %s' % (k, _fmt_value(v)))
            lines.append("")
    # Preserve unknown sections (round-trip).
    for section, v in manifest.items():
        if section in known or not isinstance(v, dict):
            continue
        lines.append("[%s]" % section)
        for k, vv in v.items():
            lines.append('%s = %s' % (k, _fmt_value(vv)))
        lines.append("")
    # Preserve top-level scalar keys.
    for k, v in manifest.items():
        if isinstance(v, dict):
            continue
        lines.append('%s = %s' % (k, _fmt_value(v)))
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _fmt_value(v) -> str:
    if isinstance(v, str):
        # BUG (deep-scan-5): backslashes were not escaped, so a value
        # containing one grew a backslash on every write/parse round-trip.
        # Deep-scan-20 fix: control characters (\n, \r, \t, \b, \f) are
        # legal TOML escapes and are DECODED by parse_manifest — but they
        # were re-emitted RAW, so a value like "note\n#1 issue" produced
        # a literal newline inside the quoted string; when the wrapped
        # continuation line contained '#', the per-line comment stripper
        # ate it and the manifest became unparseable. Escape them on
        # write like every TOML serialiser.
        out = (v.replace('\\', '\\\\').replace('"', '\\"')
                .replace("\n", "\\n").replace("\r", "\\r")
                .replace("\t", "\\t").replace("\b", "\\b")
                .replace("\f", "\\f"))
        return '"%s"' % out
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, list):
        return "[%s]" % ", ".join(_fmt_value(x) for x in v)
    if isinstance(v, dict):
        return "{ %s }" % ", ".join(
            '%s = %s' % (k, _fmt_value(x)) for k, x in v.items())
    return repr(v)


# ---------------------------------------------------------------------------
# Content hashing.
# ---------------------------------------------------------------------------



__all__ = [
    "_fmt_value",
    "_parse_value",
    "_strip_toml_comment",
    "parse_manifest",
    "write_manifest",
]
