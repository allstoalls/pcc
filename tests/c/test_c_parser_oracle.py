"""Differential test for the current C parser against the oracle
snapshots in ``tests/c_parse_oracle/``.

When the corpus or the parser changes, this test will flag the diff.
Run ``python scripts/build_c_oracle.py`` to regenerate the oracle.

This is the **parity harness** for P6C.5 de-PLY: a new C parser lands
→ swap the parser being tested here, and every diff is a real
behavioural divergence to chase.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(REPO))

from pcc.frontends.c.parse.c_parser import CParser  # noqa: E402
from pcc.frontends.c.parse.ast_normalize import normalize, diff  # noqa: E402
from tests.c_parse_oracle.corpus import CORPUS  # noqa: E402


_ORACLE_DIR = REPO / "tests" / "c_parse_oracle"

# Module-scope parser instance — CParser is expensive to build (PLY
# generates its LR table on first use), so share it across cases.
_parser = CParser()


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_c_parser_oracle(name: str) -> None:
    src = CORPUS[name]
    oracle_path = _ORACLE_DIR / f"{name}.json"
    if not oracle_path.exists():
        pytest.fail(
            f"oracle missing for {name!r} — run scripts/build_c_oracle.py"
        )
    expected = json.loads(oracle_path.read_text(encoding="utf-8"))
    tree = _parser.parse(src, filename=f"<{name}>")
    actual = normalize(tree)
    diffs = diff(expected, actual)
    if diffs:
        preview = "\n  ".join(diffs[:20])
        pytest.fail(
            f"AST divergence for {name!r} ({len(diffs)} diffs):\n  {preview}"
        )
