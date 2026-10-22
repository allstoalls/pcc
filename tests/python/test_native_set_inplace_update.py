"""Native no-libpython set in-place update methods.

Covers ``set.difference_update`` / ``set.intersection_update`` /
``set.symmetric_difference_update``. These are in-place mutators that
rewrite the receiver's contents (preserving receiver identity) using the
py_set_difference / py_set_intersection / py_set_symmetric_difference
result, and return None. They must stay on the pcc-native runtime under
``--python-libpython=off`` (i.e. never fall through to dynamic getattr /
libpython).

Slice: S-P0-SELF-SET-INPLACE-UPDATE.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


@pytest.mark.parametrize(
    "method,runtime_fn",
    [
        ("difference_update", "py_set_difference_update"),
        ("intersection_update", "py_set_intersection_update"),
        ("symmetric_difference_update", "py_set_symmetric_difference_update"),
    ],
)
@pytest.mark.parametrize("mode", ["off", "on"])
def test_inplace_update_uses_native_runtime(method, runtime_fn, mode):
    program = textwrap.dedent(
        f"""
        def f(items: list[str], drop: list[str]) -> object:
            seen = set(items)
            other = set(drop)
            seen.{method}(other)
            return seen
        """
    )
    ir = _compile_to_ll(program, f"set_{method}_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert f"@{runtime_fn}" in body, body
    # Must NOT fall through to the dynamic CPython method dispatch.
    assert f"cpy.fn.{method}" not in body, body
    assert f"cpy.call1.{method}" not in body, body


def _run_native(tmp_path, source: str, name: str) -> list[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    exe = tmp_path / name
    src.write_text(source, encoding="utf-8")
    # libpython_mode="off": prove the in-place update methods lower fully to
    # the pcc-native runtime with no libpython fallback.
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run(
        [str(exe)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return run.stdout.splitlines()


# One program that exercises all three in-place update methods, the return
# value (None), and receiver-identity preservation via an alias.
_PROGRAM = textwrap.dedent(
    """
    a = {1, 2, 3, 4}
    r = a.difference_update({2, 3})
    print(r is None)
    print(sorted(a))

    b = {1, 2, 3, 4}
    b.intersection_update({2, 3, 5})
    print(sorted(b))

    c = {1, 2, 3, 4}
    c.symmetric_difference_update({3, 4, 5, 6})
    print(sorted(c))

    # Receiver identity preserved: alias observes the in-place change.
    d = {10, 20, 30}
    e = d
    d.difference_update({20})
    print(sorted(e))

    # Empty-result path.
    g = {1, 2}
    g.intersection_update({7, 8})
    print(sorted(g))
    """
)


def test_inplace_update_runtime_matches_cpython(tmp_path):
    got = _run_native(tmp_path, _PROGRAM, "set_inplace_update")
    ref = subprocess.run(
        ["python3", "-c", _PROGRAM],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    assert got == ref, (got, ref)
