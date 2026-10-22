"""Object consumers of int() preserve Python's arbitrary-precision result."""

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python
from tests.runtime_build_cache import cached_pcc_python_runtime


@pytest.fixture
def int_object_runtime(monkeypatch):
    configured = os.environ.get("PCC_RUNTIME_ARCHIVE")
    archive = (
        Path(configured)
        if configured and os.environ.get("PCC_RUNTIME_HIGH") == "c"
        else cached_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    )
    assert archive.is_file()
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))


MINIMAL = """def object_local(value: object) -> object:
    converted: object = int(value)
    return converted

def main():
    value: object = 9223372036854775808
    print(value)
    print(object_local(value))

main()
"""


def _assert_native_matches_python(tmp_path, program):
    source = tmp_path / "object_projection.py"
    binary = tmp_path / "object_projection"
    source.write_text(program)
    reference = subprocess.run(
        [sys.executable, str(source)], text=True, capture_output=True, timeout=10
    )
    assert reference.returncode == 0, reference.stderr
    compile_python(
        str(source), str(binary), backend="self", libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(binary)], text=True, capture_output=True, timeout=10,
        env={**os.environ, "PCC_HOST_PYTHON": "/usr/bin/false"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == reference.stdout


def test_boxed_large_int_conversion_to_object_local_matches_python(
    tmp_path, int_object_runtime
):
    _assert_native_matches_python(tmp_path, MINIMAL)


CONSUMERS = """def object_local(value: object) -> object:
    converted: object = int(value)
    return converted

def object_return(value: object) -> object:
    return int(value)

def object_reassign(value: object) -> object:
    converted: object = 0
    converted = int(value)
    return converted

def string_local(value: str) -> object:
    converted: object = int(value, 0)
    return converted

def string_return(value: str) -> object:
    return int(value, 0)

def typed_identity(value: int) -> object:
    converted: object = int(value)
    return converted

def check(value: object):
    print(object_local(value))
    print(object_return(value))
    print(object_reassign(value))
    print([int(value), value])
    print((int(value), value))
    print({"converted": int(value)})
    print(value)

def main():
    check(9223372036854775808)
    check(18446744073709551615)
    check(-18446744073709551615)
    check(7)
    check(True)
    check("18446744073709551615")
    value: int = 18446744073709551615
    print(typed_identity(value))
    print(value)
    print(string_local("0xffffffffffffffff"))
    print(string_return("-0xffffffffffffffff"))
    try:
        string_local("invalid")
    except ValueError:
        print("invalid literal")

main()
"""


def test_int_object_consumer_family_matches_python(tmp_path, int_object_runtime):
    _assert_native_matches_python(tmp_path, CONSUMERS)


def test_int_object_consumers_emit_object_runtime_calls(tmp_path):
    source = tmp_path / "projection_ir.py"
    output = tmp_path / "projection_ir.ll"
    source.write_text(CONSUMERS)
    compile_python(
        str(source), str(output), backend="self", libpython_mode="off",
        ir_scaffold_mode="on", emit_llvm_only=True,
    )
    emitted = output.read_text()
    for name, runtime_name in (
        ("object_local", "py_obj_as_int_object"),
        ("object_return", "py_obj_as_int_object"),
        ("object_reassign", "py_obj_as_int_object"),
        ("string_local", "py_int_from_cstr_or_raise"),
        ("string_return", "py_int_from_cstr_or_raise"),
    ):
        body = re.search(
            r'define [^\n]*@"?[^\n"(]*' + name + r'"?\([^\n]*\)[^{]*\{(.*?)^\}',
            emitted, re.M | re.S,
        )
        assert body is not None, name
        assert runtime_name in body.group(1), name
        assert "py_int_to_i64" not in body.group(1), name


def test_typed_int_dunder_object_result_matches_python(tmp_path, int_object_runtime):
    _assert_native_matches_python(tmp_path, """class Large:
    def __int__(self) -> int:
        return 18446744073709551615

def converted(value: Large) -> object:
    result: object = int(value)
    return result

def returned(value: Large) -> object:
    return int(value)

def main():
    value = Large()
    print(converted(value))
    print(returned(value))
    print([int(value)])

main()
""")
