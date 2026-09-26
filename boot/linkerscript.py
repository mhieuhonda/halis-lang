"""Stage 84 (v0.103.0-alpha) — linker-script reading for `#![link_script]`.

A Halis image is described by a GNU-ld script: the crate declares the
script with `#![link_script("link.ld")]` and places functions into
named output sections with `#[section(".text.foo")]`. The compiler's job
is to notice, at compile time, the two ways that pairing can silently
fail:

  * the script does not place the named section — `ld` then treats the
    function as an orphan input section, parks it wherever it likes, or
    (with `--gc-sections` and no `KEEP`) drops it entirely;
  * the script file named by the crate attribute is not there, or has no
    `SECTIONS` block at all.

This module extracts, from a script, the set of input-section patterns
and output-section names the script mentions, and answers "is this
section covered?". It is a deliberately small reader for the subset of
the GNU-ld grammar a Halis image uses — not a general linker-script
parser, and not a replacement for `ld`.

The self-hosted compiler carries a line-for-line port in
`src/hlc/linkerscript.hls`; the Stage 84 acceptance gate proves the two
answer identically on the shared corpus.
"""

import re

# An output-section header looks like one of:
#     .text : ALIGN(4) { ... }
#     .bss (NOLOAD) : ALIGN(8) { ... }
#     /DISCARD/ : { ... }
#     .rodata 0x200000 : { ... }
_OUT_HEADER = re.compile(
    r"(?m)^[ \t]*(/DISCARD/|[.\w$][\w.$]*)[ \t]*(\([^)\n]*\))?[ \t]*"
    r"(0[xX][0-9a-fA-F]+|\d+)?[ \t]*:[ \t]*")

# An input-section list looks like `*(.text .text.*)` or
# `KEEP(*(.init))`; the patterns are the whitespace-separated words
# between the parentheses.
_IN_LIST = re.compile(r"\*[ \t]*\(([^)\n]*)\)")


def strip_comments(text):
    """Remove C-style `/* ... */` comments.

    Linker scripts are full of them, and an output-section name inside a
    comment is not an output section. Unterminated comments swallow the
    rest of the file, which is what a real linker does too.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                break
            # Keep newlines so line-oriented anchors still line up.
            out.append("\n" * text.count("\n", i, end + 2))
            i = end + 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def sections_block(text):
    """Return the body of the first `SECTIONS { ... }` block, or None.

    The block is found by brace matching so that a `SECTIONS` mentioned
    inside a string or a comment (comments are already gone) does not
    confuse the scan.
    """
    body = strip_comments(text)
    m = re.search(r"\bSECTIONS\b", body)
    if m is None:
        return None
    start = body.find("{", m.end())
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(body)):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return body[start + 1:i]
    return None


def placed_patterns(text):
    """Every input-section pattern and output-section name the script
    mentions inside its SECTIONS block.

    An output-section name counts as a pattern too: `.text.foo : { }`
    places a function whose `#[section]` is `.text.foo` — that is the
    point of naming an output section after the input.
    """
    block = sections_block(text)
    if block is None:
        return []
    pats = []
    for m in _OUT_HEADER.finditer(block):
        pats.append(m.group(1))
    for m in _IN_LIST.finditer(block):
        for w in m.group(1).split():
            if w:
                pats.append(w)
    return pats


def pattern_matches(pattern, name):
    """GNU ld's input-section matching rule: a pattern containing `*`
    matches by PREFIX; a pattern without `*` matches exactly."""
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return pattern == name


def covers(patterns, name):
    """True when any pattern in `patterns` places an input section
    called `name`."""
    for p in patterns:
        if pattern_matches(p, name):
            return True
    return False
