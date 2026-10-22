"""pcc-Python ports of stdlib modules.

When ``compile_python(..., recursive_stdlib=True)`` resolves an
``import X`` statement and ``pcc/ir/support/X.py`` exists, it's used in
preference to CPython's ``X.py``. This lets us provide pcc-Python
implementations of stdlib modules whose CPython source has features
pcc can't compile (C accelerators, decorators we don't yet support,
etc.) — so user code can still ``import struct`` etc. without
falling back to libpython.

The public spelling is always the CPython spelling: ``import os``,
``import gc``, ``import struct``. ``pcc/ir/support/<name>.py`` is an
implementation location selected by pcc's resolver, not a user-facing
``std.<name>`` namespace. A pcc-specific namespace would make programs
less Python-compatible and should be reserved for non-stdlib extension
APIs only.

Adding a port:
1. Create ``pcc/ir/support/<name>.py`` containing a pcc-compilable
   implementation that exposes the same public API as the CPython
   ``<name>`` module.
2. Cover the actual usage with tests so future drift can't regress
   silently.

Current ports:
- ``struct`` and helper modules used by it.
"""
