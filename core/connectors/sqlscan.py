"""core/connectors/sqlscan.py: provider-neutral SQL scanner for `:name` placeholders.

Navigation:
  scan               split SQL into (is_placeholder, text) pieces
  find_placeholders  the placeholder names in order of appearance

Skips string literals, quoted identifiers, comments, dollar-quoted bodies, and
`::` casts, so a colon inside any of those is never a placeholder. One scanner is
shared by the connector (Component 2) and the metric registry (Component 3), so
they cannot disagree about what a placeholder is.
Known limit: array slices like `a[x:y]` look like a placeholder.
"""
import re

_NAME = re.compile(r"[A-Za-z_]\w*")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def scan(sql: str) -> list[tuple[bool, str]]:
    """Return pieces `(is_placeholder, text)`. For a placeholder, text is its name
    (no colon); otherwise it is the verbatim SQL. Joining the non-placeholder
    pieces with `:name` for placeholders reproduces the input."""
    pieces: list[tuple[bool, str]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            pieces.append((False, "".join(buf)))
            buf.clear()

    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"'):  # string literal or quoted identifier ('' / "" escapes)
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i : j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
        elif c == "$" and (m := _DOLLAR_TAG.match(sql, i)):
            tag = m.group()
            j = sql.find(tag, m.end())
            j = n if j == -1 else j + len(tag)
            buf.append(sql[i:j])
            i = j
        elif c == ":":
            if sql.startswith("::", i):
                buf.append("::")
                i += 2
            elif m := _NAME.match(sql, i + 1):
                flush()
                pieces.append((True, m.group()))
                i = m.end()
            else:
                buf.append(":")
                i += 1
        else:
            buf.append(c)
            i += 1
    flush()
    return pieces


def find_placeholders(sql: str) -> list[str]:
    """Names of the `:name` placeholders, in order, repeats included."""
    return [text for is_ph, text in scan(sql) if is_ph]
