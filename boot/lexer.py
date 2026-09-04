"""Stage-0 lexer for Halis (HLS). Conforms to SPEC.md section 2."""

KEYWORDS = {
    "fn", "let", "mut", "return", "if", "else", "while", "for", "in",
    "break", "continue", "struct", "impl", "import", "uses", "true", "false",
    "enum", "match", "pure",
    # Stage 15 (v0.13.0-alpha): extern "C" — for declaring external C
    # functions. Adding `extern` as a keyword reserves the name; existing
    # programs that used `extern` as an identifier would break, but a
    # grep over the entire repo (src/, std/, examples/, tests/) shows
    # zero occurrences, so this is safe.
    "extern",
    # Stage 17 (v0.28.0-alpha): contract clauses on functions —
    # `requires <bool-expr>` (precondition) and `ensures <bool-expr>`
    # (postcondition; may reference `result`). A grep shows the words
    # only appear in comments today, so reserving them is safe.
    "requires", "ensures",
}

# BUG-29 fix: reserved identifiers (per SPEC.md section 2.5). These are
# not keywords (they don't participate in syntax) but using them as the
# name of a function, struct, enum, variable, or field raises a clear
# compile error. This prevents code written against future Stage
# additions (secure / trait) from silently breaking when those features
# land and the names become keywords.
RESERVED_IDENTIFIERS = {"secure", "trait"}

TWO_CHAR = ("->", "==", "!=", "<=", ">=", "&&", "||", "=>")
ONE_CHAR = set("(){}[],:.<>+-*/%!=?")


class HLError(Exception):
    """HLS compile error (with line:col location)."""

    def __init__(self, msg, line, col):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col

    def __str__(self):
        return "%s (line %d:%d)" % (self.msg, self.line, self.col)


def _is_ident_start(c):
    return (65 <= c <= 90) or (97 <= c <= 122) or c == 95


def _is_ident_char(c):
    return _is_ident_start(c) or (48 <= c <= 57)


def tokenize(src):
    """src: bytes -> list[dict] token {k, v, line, col}.

    k is one of: kw | ident | int | float | str | sym | eof
    v is: str (kw/ident/sym), int, float, or bytes (str)
    """
    toks = []
    i, n = 0, len(src)
    line, col = 1, 1

    def err(msg):
        raise HLError(msg, line, col)

    while i < n:
        c = src[i]
        # whitespace. Deep-scan-10 fix: a lone \r (CR-only files, opened
        # "rb" so no newline translation happens) is a LINE TERMINATOR —
        # it used to count as a single column, so every token in a
        # CR-terminated file reported line 1.
        if c in (32, 9, 13, 10):
            if c == 10:
                line += 1
                col = 1
            elif c == 13:
                # CRLF: the \n right after handles the newline; a lone
                # CR is a terminator itself.
                if i + 1 < n and src[i + 1] == 10:
                    col += 1
                else:
                    line += 1
                    col = 1
            else:
                col += 1
            i += 1
            continue
        # comment: # to end of line
        if c == 35:  # '#'
            # BUG-016 fix: track column past the comment, so error messages
            # on a later token on the same line report the right column.
            while i < n and src[i] != 10:
                i += 1
                col += 1
            continue
        # identifier / keyword
        if _is_ident_start(c):
            j = i
            while j < n and _is_ident_char(src[j]):
                j += 1
            word = src[i:j].decode("ascii")
            if word in KEYWORDS:
                toks.append({"k": "kw", "v": word, "line": line, "col": col})
            else:
                toks.append({"k": "ident", "v": word, "line": line, "col": col})
            col += j - i
            i = j
            continue
        # number
        if 48 <= c <= 57:
            j = i
            while j < n and (48 <= src[j] <= 57 or src[j] == 95):  # '_'
                j += 1
            is_float = False
            if j < n and src[j] == 46 and j + 1 < n and 48 <= src[j + 1] <= 57:
                is_float = True
                j += 1
                while j < n and (48 <= src[j] <= 57 or src[j] == 95):
                    j += 1
            # SCAN-C fix: scientific notation. After the integer or
            # fractional part, accept optional `e`/`E` + optional `+`/`-`
            # + digits. `1e10`, `1.5e-3`, `9.223372036854776e18` are now
            # lexed as a single float token instead of a float followed
            # by an `e<digits>` identifier (which caused parse errors
            # and silent mis-compilation).
            if j < n and (src[j] == 101 or src[j] == 69):  # 'e' / 'E'
                k = j + 1
                if k < n and (src[k] == 43 or src[k] == 45):  # '+' / '-'
                    k += 1
                if k < n and 48 <= src[k] <= 57:
                    # Valid exponent — consume it.
                    is_float = True
                    j = k
                    while j < n and (48 <= src[j] <= 57 or src[j] == 95):
                        j += 1
            raw_text = src[i:j].decode("ascii")
            text = raw_text.replace("_", "")
            # Deep-scan-10 fix: a number immediately followed by
            # identifier characters (e.g. `0x1F`, `123abc`) used to split
            # into two tokens with a confusing downstream error pointing
            # at the WRONG token. Reject it here with a clear message
            # (HLS has no hex literals).
            if j < n and _is_ident_start(src[j]):
                k2 = j
                while k2 < n and _is_ident_char(src[k2]):
                    k2 += 1
                raise HLError("invalid numeric literal '%s' (digits followed "
                              "by letters — HLS has no hex/adjoined-letter "
                              "literals)" % src[i:k2].decode("ascii"),
                              line, col)
            # BUG-DS4-15: keep the RAW source text on the token. Formatters
            # (hlfmt) must re-emit the original literal — str(float) can
            # produce exponent notation (1e-05) that the HLS lexer itself
            # cannot re-parse, which corrupted files on `-w`.
            if is_float:
                fv = float(text)
                # Deep-scan-10: a float literal that overflows to infinity
                # (e.g. 1e400) is rejected — SPEC §7's "every operation is
                # checked" philosophy; it used to parse silently and
                # produce inf.
                if fv == float("inf") and "inf" not in text:
                    raise HLError("float literal overflows to infinity: %s"
                                  % raw_text, line, col)
                toks.append({"k": "float", "v": fv, "raw": raw_text,
                             "line": line, "col": col})
            else:
                try:
                    iv = int(text)
                except ValueError:
                    # Python 3.11+ int-str conversion limit: literals with
                    # >= 4300 digits raise ValueError, which used to escape
                    # as a raw traceback (not an HLError) and crash the CLI.
                    raise HLError("integer literal too large: %s digits"
                                  % len(text), line, col)
                if iv > 9223372036854775808:
                    # Up to 2^63 exactly is allowed THROUGH the lexer: the
                    # parser folds `-` + `9223372036854775808` into the int64
                    # minimum; a BARE 2^63 literal is rejected by the
                    # checker's literal-range rule (fail_int_overflow_literal).
                    raise HLError("integer literal out of int64 range: %s"
                                  % raw_text, line, col)
                toks.append({"k": "int", "v": iv, "raw": raw_text,
                             "line": line, "col": col})
            col += j - i
            i = j
            continue
        # string
        if c == 34:  # '"'
            ln, cl = line, col
            i += 1
            col += 1
            out = bytearray()
            while True:
                if i >= n:
                    raise HLError("unterminated string", ln, cl)
                ch = src[i]
                if ch == 34:
                    i += 1
                    col += 1
                    break
                if ch == 92:  # '\\'
                    if i + 1 >= n:
                        raise HLError("unterminated string", ln, cl)
                    e = src[i + 1]
                    if e == 110:
                        out.append(10)
                    elif e == 116:
                        out.append(9)
                    elif e == 92:
                        out.append(92)
                    elif e == 34:
                        out.append(34)
                    else:
                        err("invalid escape sequence: \\%s" % chr(e))
                    i += 2
                    col += 2
                    continue
                if ch < 32:
                    err("string contains invalid control character")
                out.append(ch)
                col += 1
                i += 1
            toks.append({"k": "str", "v": bytes(out), "line": ln, "col": cl})
            continue
        # operator / symbol
        two = src[i:i + 2].decode("latin-1")
        if two in TWO_CHAR:
            toks.append({"k": "sym", "v": two, "line": line, "col": col})
            i += 2
            col += 2
            continue
        if chr(c) in ONE_CHAR:
            toks.append({"k": "sym", "v": chr(c), "line": line, "col": col})
            i += 1
            col += 1
            continue
        err("invalid character: %r" % chr(c))
    toks.append({"k": "eof", "v": "", "line": line, "col": col})
    return toks
