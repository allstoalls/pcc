"""Explicit-lane controls and native execution for integer proof admission."""
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


@pytest.mark.parametrize("lane", ["machine", "valueclass"])
def test_explicit_field_local_keeps_scalar_storage(tmp_path, monkeypatch, lane):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "explicit_field.py"
    definition = (
        "from dataclasses import dataclass\nfrom pcc import i64\n"
        "@dataclass\nclass Record:\n    value: i64\n"
        if lane == "machine" else
        "def valueclass(cls):\n    return cls\n"
        "@valueclass\nclass Record:\n    value: int\n"
    )
    source.write_text("from pcc.unsafe import null\n" + definition + '''def read(row: Record):
    local = row.value
    return local
def main():
    assert read(Record(17)) == 17
main()
''')
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    body = re.search(r"(?ms)^define [^\n]*@user_explicit_field_read\(.*?\n\}", text).group(0)
    assert re.search(r"%local\.addr[^ ]* = alloca i64", body)
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


def test_library_entry_does_not_use_local_only_integer_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "library_int.py"
    source.write_text('''from pcc.unsafe import null
def sum_ints(values: list[int]) -> int:
    result: int = 0
    for value in values:
        result = result + value
    return result
assert sum_ints([1, 2, 3]) == 6
''')
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    assert re.search(r"define (?:external )?ptr @user_library_int_sum_ints\(ptr", text)
    body = re.search(r"(?ms)^define [^\n]*@user_library_int_sum_ints\(.*?\n\}", text).group(0)
    assert "@py_list_get_i64_nonnegative" not in body
    assert "@py_list_get(" in body
    assert "@py_int_add" in body
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.integration
def test_ordinary_field_local_native(tmp_path, monkeypatch, pcc_runtime_archive,
                                    python_program_compiler):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "field_local_native.py"
    source.write_text('''from pcc.unsafe import null
from dataclasses import dataclass
@dataclass
class Record:
    value: int
def read(row: Record):
    local = row.value
    return local + 1
def main():
    assert read(Record(18446744073709551615)) == 18446744073709551616
    assert read(Record(-1208925819614629174706176)) == -1208925819614629174706175
    print("FIELD_LOCAL_INT_OK")
main()
''')
    binary = source.with_suffix("")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == "FIELD_LOCAL_INT_OK\n"
