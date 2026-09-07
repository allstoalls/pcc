"""Installed ``pcc`` launcher.

The console script and ``python -m pcc`` share the same dispatcher. CPython
executes this entry; pcc1 executes the native build of the same command path.
"""
from __future__ import annotations

import sys

def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Deferred on purpose: importing the 11.7k-line dispatcher at module scope
    # is host startup cost for anything that only imports this launcher, and
    # ``pcc/__main__.py`` anchors the stage1 closure on the dispatcher itself.
    from pcc.cli_bootstrap import bootstrap_cli_main

    return bootstrap_cli_main(list(argv))


if __name__ == "__main__":
    raise SystemExit(main())
