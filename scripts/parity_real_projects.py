#!/usr/bin/env python3
"""scripts/parity_real_projects.py — P6C.5 α3 gate.

Preprocesses and parses selected real-project C source files through
both parsers (legacy PLY and native driver+lexer) and checks AST
structural parity.

Usage::

    python scripts/parity_real_projects.py

Exit 0 if all samples parity-identical, 1 otherwise.

Runs the full preprocessor pipeline (pcc.frontends.c.preprocessor) on each file
so ``#include`` / typedef-name tracking works correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pcc.frontends.c.parse.c_parser import CParser  # PLY-based  # noqa: E402
from pcc.frontends.c.parse.c_parse_driver import CParseDriver  # native  # noqa: E402
from pcc.frontends.c.parse.ast_normalize import normalize, diff  # noqa: E402
from pcc.frontends.c.preprocessor import preprocess  # noqa: E402


# Small but real files from each upstream project.
# Keep the list short for the gate — broader coverage lives in the
# individual project test suites (tests/test_{nginx,lua,...}.py).
SAMPLES: list[tuple[str, str]] = [
    ("lua",        "projects/lua-5.5.0/lctype.c"),
    ("lua",        "projects/lua-5.5.0/lmem.c"),
    ("lua",        "projects/lua-5.5.0/lopcodes.c"),
    ("lua",        "projects/lua-5.5.0/lzio.c"),
    ("nginx",      "projects/nginx-1.28.3/src/core/ngx_array.c"),
    ("nginx",      "projects/nginx-1.28.3/src/core/ngx_buf.c"),
    ("nginx",      "projects/nginx-1.28.3/src/core/ngx_times.c"),
    ("lz4",        "projects/lz4-1.10.0/lib/xxhash.c"),
    ("pcre",       "projects/pcre-8.45/pcre_chartables.c"),
    ("pcre",       "projects/pcre-8.45/pcre_get.c"),
]


def main() -> int:
    _ply = CParser()
    _native = CParseDriver()

    ok = 0
    diverge: list[tuple[str, str, str, int]] = []  # (project, path, msg, diff_count)
    skip: list[tuple[str, str, str]] = []

    for project, relpath in SAMPLES:
        src_path = REPO / relpath
        if not src_path.exists():
            skip.append((project, relpath, "missing"))
            continue
        try:
            raw = src_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            skip.append((project, relpath, f"read failed: {e}"))
            continue
        # Full preprocessor: resolves #include, expands macros,
        # evaluates #if — exactly what the compile pipeline does.
        try:
            processed = preprocess(raw, base_dir=str(src_path.parent))
        except Exception as e:
            skip.append((project, relpath, f"preprocess failed: {e}"))
            continue
        try:
            ply_tree = _ply.parse(processed, filename=f"<{project}>")
        except Exception as e:
            skip.append((project, relpath, f"PLY parse failed: {e}"))
            continue
        try:
            native_tree = _native.parse(processed, filename=f"<{project}>")
        except Exception as e:
            diverge.append((project, relpath, f"native parse failed: {e}", 0))
            continue
        diffs = diff(normalize(ply_tree), normalize(native_tree))
        if diffs:
            diverge.append((
                project, relpath,
                diffs[0] if diffs else "",
                len(diffs),
            ))
        else:
            ok += 1

    print(f"parity: {ok}/{len(SAMPLES)}")
    if diverge:
        print("\ndiverging:")
        for project, path, msg, n in diverge:
            print(f"  [{project}] {path}  ({n} diffs)\n    {msg}")
    if skip:
        print("\nskipped:")
        for project, path, reason in skip:
            print(f"  [{project}] {path}  {reason}")
    return 0 if not diverge else 1


if __name__ == "__main__":
    raise SystemExit(main())
