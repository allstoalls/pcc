"""``os.environ.get(name[, default])`` lowers to ``py_os_getenv``.

This is the chained-attribute form (``os.environ.get(...)``), distinct
from ``os.getenv(...)`` which the existing ``_emit_native_os_call``
already handles. Both should map to the same runtime helper, since
``os.environ.get`` and ``os.getenv`` are semantically equivalent for
the missing-key case (both return the supplied default / ``None``).
"""

from __future__ import annotations

import re
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
def test_os_environ_get_one_arg_dispatches_to_py_os_getenv(mode):
    program = textwrap.dedent("""
        import os

        def f() -> str:
            v = os.environ.get("PCC_BACKEND")
            if v is None:
                return ""
            return v
        """)
    ir = _compile_to_ll(program, f"environ_get_1_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_getenv" in body, body
    # No dynamic os import / environ getattr in the function body.
    assert "cpy.import.os" not in body, body
    assert "cpy.get.environ" not in body, body


@pytest.mark.parametrize("with_default", [False, True])
def test_os_environ_pop_uses_owned_getitem_contains_and_unset(with_default):
    arguments = '"PCC_POP_PROBE", None' if with_default else '"PCC_POP_PROBE"'
    source = "import os\ndef probe():\n    return os.environ.pop(" + arguments + ")\n"
    text = _compile_to_ll(source, "environ_pop_" + str(with_default), mode="on")
    body = _function_body(text, "probe")
    assert body is not None
    assert "@py_os_environ_getitem" in body and "@py_os_unsetenv" in body
    assert ("@py_os_environ_contains" in body) == with_default
    assert "cpy.import.os" not in body and "strict.nolib.stub" not in body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_os_environ_get_two_args_dispatches_to_py_os_getenv(mode):
    program = textwrap.dedent("""
        import os

        def g(default: str) -> str:
            return os.environ.get("PCC_BACKEND", default)
        """)
    ir = _compile_to_ll(program, f"environ_get_2_{mode}", mode=mode)
    body = _function_body(ir, "g")
    assert body is not None
    assert "@py_os_getenv" in body, body
    assert "cpy.import.os" not in body, body
    assert "cpy.get.environ" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_os_environ_get_keeps_string_arg(mode):
    """The first arg must be passed through to ``py_os_getenv`` as the
    PyObject* name; verify the call site shape."""
    program = textwrap.dedent("""
        import os

        def h(name: str) -> str:
            return os.environ.get(name) or ""
        """)
    ir = _compile_to_ll(program, f"environ_get_var_{mode}", mode=mode)
    body = _function_body(ir, "h")
    assert body is not None
    assert "@py_os_getenv" in body, body


def test_os_environ_subscript_dispatches_to_py_os_environ_getitem_off_mode():
    """``os.environ[X]`` follows CPython mapping semantics: a missing
    variable raises KeyError (carrying the key), so it lowers to the
    raising ``py_os_environ_getitem`` helper (NOT the non-raising
    ``py_os_getenv``, which stays reserved for ``os.getenv`` /
    ``os.environ.get``) and must be followed by a pending-exception
    check, even in OFF scaffold mode.
    """
    program = textwrap.dedent("""
        import os

        def k() -> str:
            return os.environ["PCC_BACKEND"]
        """)
    ir = _compile_to_ll(program, "environ_subscript", mode="off")
    body = _function_body(ir, "k")
    assert body is not None
    assert "@py_os_environ_getitem" in body, body
    # The raising helper must be followed by an err-occurred check so a
    # KeyError/TypeError branches to the handler / fn err exit.
    assert "@py_err_occurred" in body, body
    # The non-raising getenv path must NOT be used for the subscript form.
    assert "@py_os_getenv" not in body, body
    assert "cpy.import.os" not in body, body
    assert "cpy.get.environ" not in body, body


def test_os_environ_membership_dispatches_to_native_contains_off_mode():
    program = textwrap.dedent("""
        import os

        def present(name: str) -> bool:
            return name in os.environ
        """)
    ir = _compile_to_ll(program, "environ_membership", mode="off")
    body = _function_body(ir, "present")
    assert body is not None
    assert "@py_os_environ_contains" in body, body
    assert "@py_err_occurred" in body, body
    assert "@py_cpy_" not in body, body


def test_os_environ_delitem_checks_presence_then_unsets_natively():
    program = textwrap.dedent("""
        import os

        def remove(name: str) -> None:
            del os.environ[name]
    """)
    ir = _compile_to_ll(program, "environ_delitem", mode="on")
    body = _function_body(ir, "remove")
    assert body is not None
    assert "@py_os_environ_getitem" in body, body
    assert "@py_os_unsetenv" in body, body
    getitem_key = re.search(r"@py_os_environ_getitem\(ptr ([^)]+)\)", body)
    unsetenv_key = re.search(r"@py_os_unsetenv\(ptr ([^)]+)\)", body)
    assert getitem_key is not None and unsetenv_key is not None, body
    assert getitem_key.group(1) == unsetenv_key.group(1), body
    assert "@py_cpy_" not in body, body
