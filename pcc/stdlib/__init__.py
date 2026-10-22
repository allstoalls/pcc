"""pcc self-host stdlib replacements (P6C.4).

The modules here are standalone re-implementations of the stdlib
surface pcc's own source uses. They exist so the self-hosted pcc
binary has zero CPython dependency at runtime — when pcc reads its
own source, it parses these files and compiles them like any other
user Python.

See :mod:`pcc.stdlib.README` (the ``README.md`` next to this
file) for the module inventory + implementation status.
"""
