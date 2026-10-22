"""pcc-owned locale encoding surface used by Python build tools.

The native runtime stores Python strings as UTF-8 and all currently supported
pcc hosts run in UTF-8 mode.  Meson uses :func:`getpreferredencoding` when
reading subprocess output; it does not need process-global locale mutation.
That narrow, deterministic contract is provided here.  Locale categories,
formatting, and process-global mutation are intentionally not claimed by this
build-tool port.
"""

from __future__ import annotations


def getpreferredencoding(do_setlocale: bool = True) -> str:
    # ``do_setlocale`` is intentionally ignored: pcc never mutates the
    # process-global C locale as an incidental encoding query.
    return "UTF-8"
