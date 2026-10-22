"""The two CLI helper copies must stay identical.

`pcc/driver/cli_core.py` imports the FNV-1a hashing and `.py` source-walk helpers
from `pcc/driver/cli_shared_paths.py`. `pcc/driver/cli_bootstrap.py` cannot: it compiles
with ZERO CPython fallbacks as a single translation unit
(`tests/fallback_baseline.json`), and a cross-module import turns those calls
into getattr bridges worth 47 fallbacks. So the bootstrap CLI keeps a
self-contained copy, and this contract is what makes the duplication safe:
two copies of a hash function are two chances for the run-cache key to drift
between the host CLI and the bootstrap CLI, and a content-addressed cache
cannot detect that drift on its own (AUD-P2-CLI-SHARED-HELPER-DUPLICATION).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import pcc.driver.cli_bootstrap as cli_bootstrap
import pcc.driver.cli_core as cli_core
import pcc.driver.cli_shared_paths as shared

HELPERS = ("_fnv1a_update_u64", "_fnv1a_update_bytes_u64", "_iter_py_sources_under")
FNV_OFFSET = 1469598103934665603


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


def test_cli_core_uses_the_shared_module_rather_than_a_copy():
    for name in HELPERS:
        assert getattr(cli_core, name) is getattr(shared, name), name


@pytest.mark.parametrize("name", HELPERS)
def test_bootstrap_copy_is_source_identical_to_the_shared_module(name):
    """Same code, not just same behavior — compared as parsed ASTs."""
    ours = ast.dump(ast.parse(inspect.getsource(getattr(shared, name))))
    theirs = ast.dump(ast.parse(inspect.getsource(getattr(cli_bootstrap, name))))
    assert ours == theirs, (
        f"{name} has drifted between pcc/driver/cli_shared_paths.py and the "
        "self-contained copy in pcc/driver/cli_bootstrap.py; keep them identical or "
        "the two CLIs will compute different run-cache keys"
    )


@pytest.mark.parametrize(
    "text",
    ["", "a", "pcc/driver/cli_core.py", "x" * 100, "é中文", "0" * 4096],
)
def test_text_hash_matches_across_both_copies(text):
    expected = shared._fnv1a_update_u64(FNV_OFFSET, text)
    assert cli_bootstrap._fnv1a_update_u64(FNV_OFFSET, text) == expected
    assert cli_core._fnv1a_update_u64(FNV_OFFSET, text) == expected


@pytest.mark.parametrize(
    "data", [b"", b"abc", bytes(range(256)), b"\x00" * 1024, b"\xff" * 33]
)
def test_bytes_hash_matches_across_both_copies(data):
    expected = shared._fnv1a_update_bytes_u64(FNV_OFFSET, data)
    assert cli_bootstrap._fnv1a_update_bytes_u64(FNV_OFFSET, data) == expected
    assert cli_core._fnv1a_update_bytes_u64(FNV_OFFSET, data) == expected


def test_source_walk_matches_across_both_copies():
    for root in ("pcc/backend", "pcc/frontends/python/codegen", "pcc/driver/cli_core.py"):
        target = str(_repo_root() / root)
        expected = shared._iter_py_sources_under(target)
        assert expected, f"walk of {root} returned nothing"
        assert cli_bootstrap._iter_py_sources_under(target) == expected
        assert cli_core._iter_py_sources_under(target) == expected
