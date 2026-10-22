"""pcc.stdlib.textwrap - paragraph filling and indent handling.

Scope is the surface build tools and pcc's own diagnostics use: ``dedent``,
``indent``, ``wrap``, ``fill`` and ``shorten``. Wrapping is the simple greedy
algorithm; CPython's hyphen splitting (``break_on_hyphens``) is not
implemented because no caller here uses it.

Keyword arguments are spelled out rather than collected through ``**kwargs``:
the native lowering does not accept ``**kwargs`` here, and a module that only
imports under CPython is not support.
"""
from __future__ import annotations


def dedent(text: str) -> str:
    """Remove the longest common leading whitespace from every non-blank line."""
    lines = text.split("\n")
    margin = None
    for line in lines:
        stripped = line.lstrip()
        if stripped == "":
            continue
        indent_text = line[: len(line) - len(stripped)]
        if margin is None:
            margin = indent_text
        elif indent_text.startswith(margin):
            pass
        elif margin.startswith(indent_text):
            margin = indent_text
        else:
            common = ""
            i = 0
            while i < len(margin) and i < len(indent_text):
                if margin[i] != indent_text[i]:
                    break
                common = common + margin[i]
                i = i + 1
            margin = common
    if margin is None or margin == "":
        out_blank = []
        for line in lines:
            out_blank.append("" if line.strip() == "" else line)
        return "\n".join(out_blank)
    out = []
    for line in lines:
        if line.strip() == "":
            out.append("")
        elif line.startswith(margin):
            out.append(line[len(margin) :])
        else:
            out.append(line)
    return "\n".join(out)


def indent(text: str, prefix: str) -> str:
    """Prefix every line that has content (CPython's default predicate)."""
    out = []
    for line in text.splitlines(True):
        if line.strip() == "":
            out.append(line)
        else:
            out.append(prefix + line)
    return "".join(out)


def wrap(text: str, width: int = 70, initial_indent: str = "",
         subsequent_indent: str = ""):
    """Greedy word wrap; returns lines with no trailing newlines."""
    if width <= 0:
        raise ValueError("invalid width " + str(width) + " (must be > 0)")
    words = text.split()
    if len(words) == 0:
        return []
    lines = []
    current = ""
    prefix = initial_indent
    for word in words:
        if current == "":
            candidate = prefix + word
        else:
            candidate = current + " " + word
        if len(candidate) <= width or current == "":
            current = candidate
        else:
            lines.append(current)
            prefix = subsequent_indent
            current = prefix + word
    if current != "":
        lines.append(current)
    return lines


def fill(text: str, width: int = 70, initial_indent: str = "",
         subsequent_indent: str = "") -> str:
    """``wrap`` joined by newlines."""
    return "\n".join(wrap(text, width, initial_indent, subsequent_indent))


def shorten(text: str, width: int, placeholder: str = " [...]") -> str:
    """Collapse whitespace and truncate to ``width`` with a placeholder."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= width:
        return collapsed
    if width < len(placeholder):
        raise ValueError("placeholder too large for max width")
    limit = width - len(placeholder)
    cut = collapsed[:limit]
    space = cut.rfind(" ")
    if space >= 0:
        cut = cut[:space]
    return cut.rstrip() + placeholder

