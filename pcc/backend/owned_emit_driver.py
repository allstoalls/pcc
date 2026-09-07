"""Isolated native emitter diagnostic: INPUT.pidx OUTPUT.{pco,s} KIND OPT.

OPT is 0 or 1. This exposes existing target optimizations for native
qualification without changing the compiler's current default emission tier.
"""

import sys

from pcc.backend.self_backend_indexed_emit import emit_indexed_module_file


def main() -> None:
    if len(sys.argv) != 5 or sys.argv[4] not in ("0", "1"):
        raise ValueError("expected INPUT.pidx OUTPUT KIND OPT(0|1)")
    emit_indexed_module_file(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1")


if __name__ == "__main__":
    main()
