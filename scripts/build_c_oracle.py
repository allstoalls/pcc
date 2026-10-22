#!/usr/bin/env python3
"""scripts/build_c_oracle.py — snapshot the current C parser's AST
output for each corpus entry. Run whenever the corpus changes.

Writes ``tests/c_parse_oracle/<name>.json`` for each snippet in
``tests/c_parse_oracle/corpus.py``.

Differential test harness (``tests/test_c_parser_oracle.py``) reads
these back and compares against the current parser — should be empty
diff when the parser hasn't changed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pcc.frontends.c.parse.c_parser import CParser  # noqa: E402
from pcc.frontends.c.parse.ast_normalize import normalize  # noqa: E402
from tests.c_parse_oracle.corpus import CORPUS  # noqa: E402


def main() -> int:
    oracle_dir = REPO / "tests" / "c_parse_oracle"
    parser = CParser()
    ok = 0
    failed: list[tuple[str, str]] = []
    for name, src in sorted(CORPUS.items()):
        try:
            tree = parser.parse(src, filename=f"<{name}>")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            continue
        out = oracle_dir / f"{name}.json"
        out.write_text(json.dumps(normalize(tree), indent=2, sort_keys=True))
        ok += 1
    print(f"{ok}/{len(CORPUS)} snapshots written")
    if failed:
        print("\nparse failures:")
        for name, msg in failed:
            print(f"  {name}: {msg}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
