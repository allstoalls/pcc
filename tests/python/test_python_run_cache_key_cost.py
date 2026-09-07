"""``pcc1 app.py`` run mode must not hash every byte of every source.

The run-mode cache key used to read and FNV-hash the full content of every
``.py`` file under every inferred package-site root.  For one ordinary root set
(13302 files, 236 MB) that cost 29.3 s on CPython, and pcc1's compiled byte
loop turned ``pcc1 app.py`` into an apparent hang: 2463 of 2487 samples sat in
``_python_run_cache_key``.  A run cache only has to notice that a source
changed, so the key hashes each file's relative path, size and
``st_mtime_ns``.  These tests pin both halves: the cost is linear in the file
count, and an edit still invalidates the cache.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pcc import cli_bootstrap

_KEY_ARGS = {
    "python_libpython": "off",
    "python_library": False,
    "ir_scaffold": "on",
    "backend": "self",
}


def _tree(root: Path, files: int, bytes_each: int) -> Path:
    package = root / "wide_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    body = "# " + ("x" * bytes_each) + "\n"
    for index in range(files):
        (package / f"mod_{index}.py").write_text(body, encoding="utf-8")
    entry = root / "app.py"
    entry.write_text("print('hi')\n", encoding="utf-8")
    return entry


def _key(entry: Path) -> str:
    return cli_bootstrap._python_run_cache_key(str(entry), **_KEY_ARGS)


def test_the_key_does_not_read_file_contents() -> None:
    source = Path(cli_bootstrap.__file__).read_text(encoding="utf-8")
    start = source.index("def _python_run_cache_key(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "_fnv1a_update_bytes_u64" not in body, "the key must not hash file bytes"
    assert "st_mtime_ns" in body and "st_size" in body
    assert "seen = {}" in body, "membership must not be a linear list scan"


def test_cost_is_linear_in_file_count_not_in_bytes(tmp_path: Path) -> None:
    small = _tree(tmp_path / "small", 400, 200)
    fat = _tree(tmp_path / "fat", 400, 200_000)
    os.environ["PCC_PACKAGE_SITE"] = str(tmp_path / "small")
    try:
        started = time.time()
        _key(small)
        small_seconds = time.time() - started
        os.environ["PCC_PACKAGE_SITE"] = str(tmp_path / "fat")
        started = time.time()
        _key(fat)
        fat_seconds = time.time() - started
    finally:
        os.environ.pop("PCC_PACKAGE_SITE", None)
    # 1000x the bytes, same file count: content hashing showed up as a
    # proportional slowdown, identity hashing does not.
    assert fat_seconds < small_seconds * 8 + 1.0, (small_seconds, fat_seconds)


def test_an_edit_changes_the_key(tmp_path: Path) -> None:
    entry = _tree(tmp_path / "edit", 8, 64)
    os.environ["PCC_PACKAGE_SITE"] = str(tmp_path / "edit")
    try:
        before = _key(entry)
        target = tmp_path / "edit" / "wide_pkg" / "mod_3.py"
        original = target.read_text(encoding="utf-8")
        target.write_text(original + "VALUE = 1\n", encoding="utf-8")
        grew = _key(entry)
        # Same size, new mtime: rewrite the original content with a later stamp.
        target.write_text(original, encoding="utf-8")
        os.utime(target, ns=(1, 1_700_000_000_000_000_000))
        restamped = _key(entry)
    finally:
        os.environ.pop("PCC_PACKAGE_SITE", None)
    assert before != grew
    assert before != restamped
