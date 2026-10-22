"""Native ``dict.setdefault(key)`` 1-arg (default ``None``) dispatch.

Previously only the 2-arg form ``d.setdefault(k, default)`` was lowered
natively; the 1-arg form ``d.setdefault(k)`` fell through to a dynamic
getattr and was rejected under ``--python-libpython=off``. CPython inserts
``{k: None}`` for a missing key and returns ``None``; for a present key it
returns the existing value and does not overwrite it. See slice
S-P0-SELF-DICT-SETDEFAULT-1ARG.
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


@pytest.mark.parametrize("mode", ["off", "on"])
def test_dict_setdefault_1arg_uses_native_runtime(mode):
    """1-arg ``setdefault`` lowers to py_dict_get + py_dict_set, no cpy
    fallback."""
    program = textwrap.dedent("""
        def f(values: dict[str, object], key: str) -> object:
            return values.setdefault(key)
        """)
    ir = _compile_to_ll(program, f"dict_setdefault_1arg_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_get" in body, body
    assert "@py_dict_set" in body, body
    assert "cpy.fn.setdefault" not in body, body
    assert "cpy.call1.setdefault" not in body, body


def test_dyn_dict_setdefault_1arg_uses_native_runtime():
    """The DynType-dict path (``values`` untyped) also routes the 1-arg
    form through the native runtime, not a dynamic getattr."""
    program = textwrap.dedent("""
        def f(values, key):
            return values.setdefault(key)
        """)
    ir = _compile_to_ll(program, "dyn_dict_setdefault_1arg", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_get" in body, body
    assert "@py_dict_set" in body, body
    assert "cpy.fn.setdefault" not in body, body


def test_dict_setdefault_1arg_runtime_matches_cpython(tmp_path):
    """no-libpython self-backend compile + run; output must match python3.

    Covers: missing key -> insert None + return None; present key ->
    return existing value, no overwrite; interaction with a later lookup.

    The dict is annotated ``dict[str, object]`` so its value type is not a
    native scalar. The scalar-valued (inferred ``dict[str, int]``) case is
    covered by ``test_dict_setdefault_1arg_scalar_valued_dict_is_none_gap``.
    """
    from pcc.frontends.python.pipeline import compile_python

    program = textwrap.dedent("""
        def main() -> None:
            d: dict[str, object] = {"a": 1}
            r1 = d.setdefault("a")     # present -> returns 1, no overwrite
            print(r1)
            print(d["a"])
            r2 = d.setdefault("b")     # missing -> inserts None, returns None
            print(r2)
            print("b" in d)
            print(d["b"] is None)
            r3 = d.setdefault("c", 5)  # 2-arg still works
            print(r3)
            print(d["c"])
            print(len(d))
        main()
    """).lstrip()

    src = tmp_path / "setdefault_1arg.py"
    src.write_text(program, encoding="utf-8")
    exe = tmp_path / "setdefault_1arg.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    got = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert got.returncode == 0, got.stderr

    ref = subprocess.run(
        ["python3", str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ref.returncode == 0, ref.stderr
    assert got.stdout == ref.stdout, (got.stdout, ref.stdout)
    # Pin the expected reference so a python3 regression is also caught.
    assert got.stdout == "1\n1\nNone\nTrue\nTrue\n5\n5\n3\n", got.stdout


def test_dict_setdefault_1arg_scalar_valued_dict_is_none_gap(tmp_path):
    """Unannotated ``{"a": 1}`` (inferred dict[str, int]): ``is None`` on
    the setdefault result / stored slot must match CPython (True).

    Was a strict xfail (scalar-valued dict typing hole). Fixed in
    ``type_infer.py``: a local that receives a 1-arg ``setdefault``
    somewhere in the function widens its inferred dict value type to
    ``dyn`` (``_collect_setdefault_none_receivers`` + ``_infer_assign``),
    and 1-arg ``get``/``setdefault`` results on scalar-valued dicts are
    typed ``dyn``, so the ``is None`` compare is a real pointer compare
    instead of constant-folding to False."""
    from pcc.frontends.python.pipeline import compile_python

    program = textwrap.dedent("""
        def main() -> None:
            d = {"a": 1}
            r2 = d.setdefault("b")
            print(r2 is None)
            print(d["b"] is None)
        main()
    """).lstrip()

    src = tmp_path / "setdefault_1arg_scalar_dict.py"
    src.write_text(program, encoding="utf-8")
    exe = tmp_path / "setdefault_1arg_scalar_dict.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    got = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert got.returncode == 0, got.stderr
    assert got.stdout == "True\nTrue\n", got.stdout
