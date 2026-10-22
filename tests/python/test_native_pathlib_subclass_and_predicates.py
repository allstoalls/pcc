"""pathlib must distinguish files from directories and keep its subclass.

Two defects, both of which made the compiled compiler mis-identify itself:

``is_file`` and ``is_dir`` both answered ``exists()``.  A directory was a
file and a file was a directory.  ``pcc/driver/bootstrap_cache_identity.py`` branches
on ``entry.is_file()`` to choose between "hash this one file" and "walk this
tree", so compiled it hashed 2 paths where the host hashed 606.

``__truediv__``, ``parent``, ``with_suffix`` and ``with_name`` constructed
``PurePath`` rather than the receiver's class, so ``Path(root) / "pcc"`` came
back without any filesystem method at all.

Downstream both showed up as one message.  ``codegen_checksum`` wraps the
identity computation in ``except Exception: value = "unknown"``; an unknown
identity is fail-closed stale; so pcc1 rejected every runtime archive with
"pcc-Python runtime archive has stale codegen provenance" and could not
compile anything.

CPython is the oracle: the host ``pathlib`` answers are computed here, not
written down.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import pathlib as host_pathlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = REPO_ROOT / "pcc" / "stdlib" / "pathlib.py"


def _provider():
    spec = importlib.util.spec_from_file_location("pcc_pathlib_provider", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _samples():
    stdlib = REPO_ROOT / "pcc" / "stdlib"
    return [
        str(stdlib),
        str(stdlib / "json.py"),
        str(stdlib / "urllib"),
        str(stdlib / "does-not-exist"),
        "/",
    ]


def test_predicates_distinguish_files_from_directories():
    provider = _provider()
    for path in _samples():
        for name in ("exists", "is_file", "is_dir"):
            expected = getattr(host_pathlib.Path(path), name)()
            actual = getattr(provider.Path(path), name)()
            assert actual == expected, f"{name}({path}) -> {actual}, host {expected}"
    stdlib = REPO_ROOT / "pcc" / "stdlib"
    assert provider.Path(str(stdlib)).is_dir()
    assert not provider.Path(str(stdlib)).is_file()
    assert provider.Path(str(stdlib / "json.py")).is_file()
    assert not provider.Path(str(stdlib / "json.py")).is_dir()


def test_combining_preserves_the_concrete_class():
    provider = _provider()
    root = provider.Path(str(REPO_ROOT))
    child = root / "pcc" / "stdlib" / "json.py"
    assert isinstance(child, provider.Path), type(child).__name__
    assert child.is_file()
    for value in (child.parent, child.with_suffix(".bak"),
                  child.with_name("other.py"), root.parents[0]):
        assert isinstance(value, provider.Path), type(value).__name__
    # A PurePath stays pure; only the concrete class gains the predicates.
    assert not isinstance(provider.PurePath("/a") / "b", provider.Path)


def test_relative_to_matches_the_host():
    provider = _provider()
    base = str(REPO_ROOT / "pcc" / "stdlib")
    for path in (base + "/json.py", base, base + "/urllib/parse.py", base + "/urllib"):
        expected = str(host_pathlib.Path(path).relative_to(base))
        assert str(provider.Path(path).relative_to(base)) == expected
    with pytest.raises(ValueError):
        provider.Path("/tmp/elsewhere").relative_to(base)
    with pytest.raises(ValueError):
        host_pathlib.Path("/tmp/elsewhere").relative_to(base)


def test_glob_and_rglob_match_the_host():
    provider = _provider()
    base = str(REPO_ROOT / "pcc" / "stdlib")
    for pattern in ("*.py", "*", "js*.py"):
        expected = sorted(str(p) for p in host_pathlib.Path(base).glob(pattern))
        actual = sorted(str(p) for p in provider.Path(base).glob(pattern))
        assert actual == expected, pattern
    for pattern in ("*.py", "*"):
        expected = sorted(str(p) for p in host_pathlib.Path(base).rglob(pattern))
        actual = sorted(str(p) for p in provider.Path(base).rglob(pattern))
        assert actual == expected, pattern
    assert all(
        isinstance(p, provider.Path)
        for p in provider.Path(base).glob("*.py")
    )


def test_name_matcher_agrees_with_fnmatch_on_star_and_question():
    """The matcher is deliberately narrower than fnmatch; pin where it stops."""
    provider = _provider()
    patterns = ["*", "*.py", "self_backend*.py", "a?c", "*x*", "abc", "?", "a*b*c"]
    names = ["abc", "a.py", "self_backend_ir.py", "axc", "xyz", "x", "", "aXbYc", "ab"]
    for pattern in patterns:
        for name in names:
            assert provider.Path._name_matches(name, pattern) == fnmatch.fnmatchcase(
                name, pattern
            ), (name, pattern)
    with pytest.raises(ValueError):
        provider.Path._name_matches("abc", "[ab]c")


def test_bootstrap_identity_sees_the_whole_source_set():
    """The end the defects were found through, asserted directly."""
    from pcc.driver.bootstrap_cache_identity import bootstrap_source_files, repo_root

    base = repo_root()
    files = bootstrap_source_files(base)
    assert len(files) > 500, f"only {len(files)} source files"
    assert all(Path(str(f)).is_file() for f in files)
