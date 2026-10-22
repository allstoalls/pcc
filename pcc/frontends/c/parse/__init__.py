"""pcc.frontends.c.parse — C parser entry point.

Default path: native LR driver + native lexer (P6C.5 α1 + α2).
Opt-out: ``PCC_USE_PLY_C_PARSER=1`` routes through the legacy
PLY-based ``pcc.frontends.c.parse.c_parser.CParser``.

Use ``make_c_parser()`` from pcc internals instead of instantiating
``CParser`` directly — that factory respects the env var gate.
"""
from __future__ import annotations

import os


def make_c_parser():
    """Return a C parser honoring the ``PCC_USE_PLY_C_PARSER`` flag.

    The two parsers expose a compatible ``parse(text, filename='')``
    method returning a ``c_ast.FileAST``. Behavioural parity is gated
    by ``tests/test_c_parse_driver_parity.py`` (63/63 as of α2).
    """
    if os.environ.get("PCC_USE_PLY_C_PARSER") == "1":
        from pcc.frontends.c.parse.c_parser import CParser
        return CParser()
    from pcc.frontends.c.parse.c_parse_driver import CParseDriver
    return CParseDriver()
