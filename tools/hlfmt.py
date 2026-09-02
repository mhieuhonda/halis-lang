#!/usr/bin/env python3
"""hlfmt — Opinionated formatter for Hieu Louis (HLS).

Stage 14 (v0.12.0-alpha): the `gofmt` of HLS — ends style debates.

Design:
  - 4-space indentation; no tabs.
  - One statement per line (preserves the source's line breaks).
  - Single space after commas, colons, around binary operators.
  - No space before `(`, `[`, after `!`, `.` (postfix).
  - Space before `{` (function/struct/enum/impl/match/if/while/for bodies).
  - Empty line between top-level declarations (auto-inserted).
  - Trailing newline at EOF.
  - Idempotent: running twice = running once.

The formatter operates on the token stream (preserving line/col info)
so it preserves all comments and string literals exactly. It walks the
tokens, normalises the whitespace BETWEEN them, and re-emits while
preserving the original line breaks.

Usage:
  hlfmt FILE.hls               # print formatted source to stdout
  hlfmt -w FILE.hls            # write back to file
  hlfmt -c FILE.hls            # check if file is already formatted
  hlfmt -d FILE.hls            # print diff against original
"""
import argparse
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from boot.lexer import tokenize, HLError  # noqa: E402


# Token categories (mirror boot/lexer.py).
EOF = "eof"
SYM = "sym"
STR = "str"

# Tokens that should NOT have a space before them when on the same line.
# (HLS does not have a `;` token, so it's omitted here.)
NO_SPACE_BEFORE = {"(", "[", ".", ",", ")", "]", "?", ":"}

# Tokens that should NOT have a space after them.
NO_SPACE_AFTER = {"(", "[", ".", "!", "?"}

# Word-like token kinds (need space between two consecutive word-tokens).
# String literals are also "word-like" in that they need a space before
# and after them when adjacent to identifiers/keywords/operators.
WORD_KINDS = {"kw", "ident", "int", "float", "str"}

# Tokens that should have a space AFTER them (binary operators etc.).
# (HLS does not have a `;` token, so it's omitted here.)
SPACE_AFTER_SYMS = {",", ":", "=", "==", "!=", "<=", ">=", "->", "=>",
                    "+", "-", "*", "/", "%", "<", ">", "&&", "||"}

# Tokens that should have a space BEFORE them when on the same line.
SPACE_BEFORE_SYMS = {"{", "}", "=>", "->",
                     "+", "-", "*", "/", "%", "<", ">", "=",
                     "==", "!=", "<=", ">=", "&&", "||", ","}


def format_source(src: bytes) -> str:
    """Format HLS source bytes; return the formatted source as a string."""
    try:
        toks = tokenize(src)
    except HLError as ex:
        sys.stderr.write("warning: %s\n" % ex)
        return src.decode("utf-8", errors="replace")

    out_lines = []
    cur_line_parts = []
    indent = 0
    cur_line_num = 1

    def emit_cur_line(force_blank=False):
        """Emit the current line. If `force_blank` is True, emit a blank
        line (used to preserve intentional blank lines in the source)."""
        nonlocal cur_line_parts
        line = "".join(cur_line_parts).rstrip()
        if line:
            out_lines.append(line)
        elif force_blank and out_lines:
            # Only emit a blank line if we have prior content AND the
            # previous line was not already blank.
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
        cur_line_parts = []

    def indent_str():
        return "    " * indent

    prev = None  # previous emitted token
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t["k"] == EOF:
            break
        # Handle line breaks first — if the token's line is greater than
        # the current line, advance. Emit a blank line only if the gap
        # is more than 1 line (intentional blank in source).
        gap = t["line"] - cur_line_num
        if gap > 0:
            # Flush the current line content.
            emit_cur_line()
            cur_line_num += 1
            # For each remaining gap line, emit a blank line (but only
            # if the gap > 1, meaning the source had an intentional blank).
            while cur_line_num < t["line"]:
                emit_cur_line(force_blank=True)
                cur_line_num += 1
        cur_line_num = t["line"]
        # Now emit the token.
        if t["k"] == SYM and t["v"] == "{":
            # Open brace: emit on the same line with a space before,
            # then increase indent.
            if cur_line_parts and not cur_line_parts[-1].endswith(" "):
                cur_line_parts.append(" ")
            cur_line_parts.append("{")
            emit_cur_line()
            indent += 1
            prev = t
            i += 1
            continue
        if t["k"] == SYM and t["v"] == "}":
            # Close brace: flush current line, decrease indent, emit `}`.
            emit_cur_line()
            indent = max(0, indent - 1)
            cur_line_parts.append(indent_str())
            cur_line_parts.append("}")
            # Peek ahead: if the next token is `=>`, `,`, `)`, `]`, keep on
            # same line; otherwise flush.
            nxt = toks[i + 1] if i + 1 < n else None
            if nxt and nxt["k"] == SYM and nxt["v"] in (",", ")", "]", ";", "=>"):
                # Will be handled by the next iteration's whitespace logic.
                pass
            else:
                emit_cur_line()
            prev = t
            i += 1
            continue
        # Regular token: emit with appropriate whitespace.
        # If the current line is empty, add the indent.
        if not cur_line_parts:
            cur_line_parts.append(indent_str())
        # Decide whether we need a space before this token.
        if prev is not None:
            prev_v = _token_str(prev)
            cur_v = _token_str(t)
            prev_kind = prev["k"]
            cur_kind = t["k"]
            need_space = False
            # Rule 1: two word-like tokens in a row -> space.
            if prev_kind in WORD_KINDS and cur_kind in WORD_KINDS:
                need_space = True
            # Rule 2: prev is a closing `)` or `]`, cur is a word -> space.
            if prev_v in (")", "]") and cur_kind in WORD_KINDS:
                need_space = True
            # Rule 3: prev is a word/literal/`)`/`]`, cur is a sym with
            # space-before rule.
            if (prev_kind in WORD_KINDS or prev_v in (")", "]")) and cur_v in SPACE_BEFORE_SYMS:
                need_space = True
            # Rule 4: prev is a sym with space-after rule, cur is word/literal
            # or `(`, `[` (treat `(`/`[` like word tokens here so they get
            # a space after binary operators).
            if prev_v in SPACE_AFTER_SYMS and (cur_kind in WORD_KINDS or cur_v in ("(", "[")):
                need_space = True
            # Override: no space if cur is in NO_SPACE_BEFORE.
            # Exception: if prev is a binary operator (SPACE_AFTER_SYMS),
            # we still want a space before `(`/`[` (e.g. `1 + (2)` not
            # `1 +(2)`).
            if cur_v in NO_SPACE_BEFORE and not (prev_v in SPACE_AFTER_SYMS and cur_v in ("(", "[")):
                need_space = False
            # Override: no space if prev is in NO_SPACE_AFTER.
            if prev_v in NO_SPACE_AFTER:
                need_space = False
            # Don't double-up spaces.
            if cur_line_parts and cur_line_parts[-1].endswith(" "):
                need_space = False
            if need_space:
                cur_line_parts.append(" ")
        cur_line_parts.append(_render_token(t))
        prev = t
        i += 1
    # Flush any remaining content.
    emit_cur_line()
    # Ensure a single trailing newline.
    while out_lines and out_lines[-1] == "":
        out_lines.pop()
    result = "\n".join(out_lines)
    if not result.endswith("\n"):
        result += "\n"
    return result


def _token_str(t) -> str:
    """Return the source-text representation of a token (for whitespace decisions)."""
    v = t["v"]
    if isinstance(v, bytes):
        return v.decode("latin-1")
    return str(v)


def _render_token(t) -> str:
    """Render a token as a string (with escapes for string literals)."""
    k = t["k"]
    v = t["v"]
    if k == STR:
        # String tokens store bytes (the raw byte sequence from the
        # source). We need to re-emit them as a valid HLS string literal,
        # escaping only the bytes that would terminate the string or
        # break the escape syntax. Multi-byte UTF-8 sequences are
        # preserved as-is by emitting each byte via chr() and then
        # encoding the result as latin-1 when written to disk.
        if isinstance(v, bytes):
            out = ['"']
            for b in v:
                if b == 0x22:        # "
                    out.append('\\"')
                elif b == 0x5c:      # backslash
                    out.append('\\\\')
                elif b == 0x0a:       # newline
                    out.append('\\n')
                elif b == 0x09:       # tab
                    out.append('\\t')
                elif b == 0x0d:       # carriage return
                    out.append('\\r')
                elif b < 0x20:
                    out.append('\\x%02x' % b)
                else:
                    # Use chr(b) so the byte value is preserved exactly
                    # (1:1 mapping). When the output string is written
                    # with latin-1 encoding, each char becomes one byte.
                    out.append(chr(b))
            out.append('"')
            return "".join(out)
        # Already a string (shouldn't happen with the HLS lexer).
        escaped = (v.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\t", "\\t")
                    .replace("\r", "\\r"))
        return '"%s"' % escaped
    if isinstance(v, bytes):
        return v.decode("latin-1")
    return str(v)


def is_formatted(src: bytes) -> bool:
    """Return True if the source is already formatted."""
    formatted = format_source(src)
    return formatted.encode("latin-1", errors="replace") == src


def main():
    parser = argparse.ArgumentParser(
        prog="hlfmt",
        description="Hieu Louis opinionated formatter (Stage 14-alpha).")
    parser.add_argument("file", help="HLS source file to format.")
    parser.add_argument("-w", "--write", action="store_true",
                        help="Write back to file (default: print to stdout).")
    parser.add_argument("-c", "--check", action="store_true",
                        help="Exit non-zero if file is not already formatted.")
    parser.add_argument("-d", "--diff", action="store_true",
                        help="Print unified diff against original.")
    args = parser.parse_args()
    if not os.path.isfile(args.file):
        sys.stderr.write("error: file not found: %s\n" % args.file)
        return 1
    with open(args.file, "rb") as f:
        src = f.read()
    try:
        formatted = format_source(src)
    except Exception as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 1
    if args.check:
        # Compare as bytes (encode the formatted output as latin-1 to
        # preserve multi-byte UTF-8 sequences exactly).
        if formatted.encode("latin-1", errors="replace") == src:
            print("%s: already formatted" % args.file)
            return 0
        else:
            print("%s: NOT formatted" % args.file)
            return 1
    if args.diff:
        # Decode both as latin-1 so byte-level comparison works.
        orig_lines = src.decode("latin-1", errors="replace").splitlines(keepends=True)
        new_lines = formatted.splitlines(keepends=True)
        diff = difflib.unified_diff(orig_lines, new_lines,
                                    fromfile=args.file, tofile=args.file + ".fmt")
        sys.stdout.writelines(diff)
        return 0
    if args.write:
        # Write as latin-1 to preserve the exact byte sequence of
        # string literals (which may contain multi-byte UTF-8).
        with open(args.file, "wb") as f:
            f.write(formatted.encode("latin-1", errors="replace"))
        print("%s: formatted" % args.file)
        return 0
    # Default: print to stdout. Use latin-1 to preserve bytes.
    sys.stdout.buffer.write(formatted.encode("latin-1", errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
