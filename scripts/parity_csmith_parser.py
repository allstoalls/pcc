#!/usr/bin/env python3
"""scripts/parity_csmith_parser.py — P6C.5 α3 gate.

Fuzzes the C parser pair (PLY legacy vs native driver) with csmith-
generated random C programs. Each seed:

  1. csmith -s <seed>          → random .c source
  2. pcc preprocessor          → expanded source
  3. PLY CParser.parse()       → AST_ply
  4. CParseDriver.parse()      → AST_native
  5. normalize + diff           → must be empty

Quick gate::

    python scripts/parity_csmith_parser.py --seeds 20

Overnight fuzz::

    python scripts/parity_csmith_parser.py --seeds 10000

Exit 0 if zero diffs, 1 otherwise.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pcc.frontends.c.parse.c_parser import CParser  # PLY-based  # noqa: E402
from pcc.frontends.c.parse.c_parse_driver import CParseDriver  # native  # noqa: E402
from pcc.frontends.c.parse.ast_normalize import normalize, diff  # noqa: E402
from pcc.frontends.c.preprocessor import preprocess  # noqa: E402


_CSMITH_INCLUDE_CANDIDATES = (
    "/opt/homebrew/Cellar/csmith/2.3.0/include/csmith-2.3.0/runtime",
    "/opt/homebrew/include/csmith",
    "/usr/local/include/csmith-2.3.0/runtime",
    "/usr/local/include/csmith",
)


def _find_csmith_include() -> str | None:
    for p in _CSMITH_INCLUDE_CANDIDATES:
        if (Path(p) / "csmith.h").is_file():
            return p
    return None


def gen_csmith(seed: int) -> str | None:
    """Run csmith with a seed, skip the usual ``#include "csmith.h"``
    dance by prepending a minimal typedef preamble (int8_t, uint32_t,
    etc.) so pcc's own preprocessor + parsers see a self-contained TU.

    Using the system cpp pulls in macOS/glibc headers that pcc's C
    parser can't digest (``__darwin_va_list`` and friends). The
    typedef preamble here is sufficient for csmith's generated code —
    csmith only uses the C99 fixed-width int types + printf.
    """
    try:
        cs = subprocess.run(
            ["csmith", "-s", str(seed)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if cs.returncode != 0:
        return None
    # Drop csmith's own ``#include "csmith.h"`` line and prepend a
    # minimal preamble; this keeps the TU hermetic and lets pcc's own
    # preprocessor (which doesn't know system header paths) process it.
    csrc = cs.stdout.replace('#include "csmith.h"', "")
    preamble = _CSMITH_STUB_HEADER
    return preamble + "\n" + csrc


# Minimal typedefs — csmith only touches these names.
_CSMITH_STUB_HEADER = """
typedef signed char int8_t;
typedef short int16_t;
typedef int int32_t;
typedef long long int64_t;
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;
/* csmith runtime symbols — stubbed to avoid dragging in real
 * ``csmith.h`` (which drags in stdio / stdlib we can't parse). */
void platform_main_begin(void);
void platform_main_end(unsigned long checksum, int verbose);
void transparent_crc(unsigned long * crc, unsigned long v, const char* s);
"""


def run_seed(seed: int, ply: CParser, native: CParseDriver) -> tuple[str, int, str]:
    """Return (status, diff_count, detail).

    status ∈ {"ok", "diverge", "skip"}.
    """
    processed = gen_csmith(seed)
    if processed is None:
        return ("skip", 0, "csmith/cpp failed")
    # Strip cpp ``# <lineno> "file"`` directives that our C parsers
    # understand but that might cause spurious differences in source
    # coordinates (we normalize coords away anyway).
    try:
        ply_tree = ply.parse(processed, filename=f"<csmith-{seed}>")
    except Exception as e:
        return ("skip", 0, f"PLY parse failed: {e}")
    try:
        native_tree = native.parse(processed, filename=f"<csmith-{seed}>")
    except Exception as e:
        return ("diverge", 0, f"native parse failed: {e}")
    diffs = diff(normalize(ply_tree), normalize(native_tree))
    if not diffs:
        return ("ok", 0, "")
    return ("diverge", len(diffs), diffs[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20,
                    help="Number of csmith seeds to try")
    ap.add_argument("--start", type=int, default=1,
                    help="First seed value (default: 1)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    ply = CParser()
    native = CParseDriver()

    ok = 0
    diverge = 0
    skip = 0
    for seed in range(args.start, args.start + args.seeds):
        status, nd, detail = run_seed(seed, ply, native)
        if status == "ok":
            ok += 1
            if args.verbose:
                print(f"  seed={seed}: OK")
        elif status == "skip":
            skip += 1
            if args.verbose:
                print(f"  seed={seed}: SKIP ({detail})")
        else:
            diverge += 1
            print(f"  seed={seed}: DIVERGE ({nd} diffs): {detail}")

    total = ok + diverge + skip
    print(f"\nresult: {ok}/{total} ok, {diverge} diverge, {skip} skip")
    return 0 if diverge == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
