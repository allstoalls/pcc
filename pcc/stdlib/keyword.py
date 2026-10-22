"""Python 3.15 keyword membership for the owned stdlib.

The public lists are mutable, while the membership predicates retain the
immutable snapshots created at module initialization, as CPython does.
"""

__all__ = ["iskeyword", "issoftkeyword", "kwlist", "softkwlist"]

kwlist = [
    "False",
    "None",
    "True",
    "and",
    "as",
    "assert",
    "async",
    "await",
    "break",
    "class",
    "continue",
    "def",
    "del",
    "elif",
    "else",
    "except",
    "finally",
    "for",
    "from",
    "global",
    "if",
    "import",
    "in",
    "is",
    "lambda",
    "nonlocal",
    "not",
    "or",
    "pass",
    "raise",
    "return",
    "try",
    "while",
    "with",
    "yield",
]

softkwlist = ["_", "case", "lazy", "match", "type"]

_keyword_set = frozenset(kwlist)
_softkeyword_set = frozenset(softkwlist)


def iskeyword(key, /) -> bool:
    return key in _keyword_set


def issoftkeyword(key, /) -> bool:
    return key in _softkeyword_set
