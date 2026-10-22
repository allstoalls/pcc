"""Ordinary integer object boundaries remain exact in scaffold modules."""
import contextlib
import io
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


CONSTRUCTORS = '''from dataclasses import dataclass
from pcc.unsafe import null
class Box:
    def __init__(self, value: int):
        self.value = value
@dataclass(frozen=True)
class Row:
    value: int
    other: int = 0
def main():
    value = 18446744073709551615
    assert Box(value).value == value
    assert Box(18446744073709551615).value == value
    assert Row(value=18446744073709551615).value == value
    assert Row(*(value,), **{"other": -(1 << 100)}).other == -(1 << 100)
    print("ORDINARY_INT_BOUNDARIES_OK")
main()
'''

UNPACK = '''from pcc.unsafe import null
def main():
    pairs = ((9223372036854775808, "first"), (18446744073709551615, "second"))
    values = []
    for value, label in pairs:
        values.append(value)
    assert values == [9223372036854775808, 18446744073709551615]
    pair = (-(1 << 100), 1 << 100)
    left, right = pair
    assert left == -(1 << 100) and right == 1 << 100
    values = []
    for value in (9223372036854775808, 18446744073709551615):
        values.append(value)
    assert values == [9223372036854775808, 18446744073709551615]
    print("ORDINARY_INT_BOUNDARIES_OK")
main()
'''

PARAMETERS = '''from pcc.unsafe import null
def check_uint(value: int, bits: int) -> int:
    if value < 0 or value >= (1 << bits):
        raise ValueError("out of range")
    return value
def produce() -> int:
    return 18446744073709551615
class Echo:
    def accept(self, value: int) -> int:
        return value
def main():
    assert check_uint(18446744073709551615, 64) == 18446744073709551615
    assert produce() == 18446744073709551615
    assert Echo().accept(1 << 100) == 1 << 100
    rejected = 0
    for invalid in (-1, 18446744073709551616):
        try:
            check_uint(invalid, 64)
        except ValueError:
            rejected += 1
    assert rejected == 2
    print("ORDINARY_INT_BOUNDARIES_OK")
main()
'''

PROGRAMS = {"constructors": CONSTRUCTORS, "unpack": UNPACK, "parameters": PARAMETERS}


def test_constructor_export_keeps_ordinary_int_object_abi(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    source = tmp_path / "provider.py"
    source.write_text(CONSTRUCTORS.split("def main():", 1)[0])
    _parsed, exports, _derived = build_closed_world_context([str(source)], ["provider"])
    for cls in ("Box", "Row"):
        method = next(item for item in exports["provider"][cls]["methods"]
                      if item["name"] == "__init__")
        assert method["box_int_abi"] is True


@pytest.mark.parametrize("marker", ["__pcc_freestanding__", "__pcc_runtime_port__"])
def test_manual_integer_abi_includes_private_helpers(tmp_path, monkeypatch, marker):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "manual_abi.py"
    source.write_text(marker + ''' = True
from pcc.extern import c_abi_export
def private_helper(value: int) -> int:
    return value + 1
@c_abi_export("manual_probe")
def exported(value: int) -> int:
    return private_helper(value)
''')
    _parsed, exports, _derived = build_closed_world_context([str(source)], ["manual_abi"])
    assert exports["manual_abi"]["private_helper"]["box_int_abi"] is False
    assert exports["manual_abi"]["exported"]["box_int_abi"] is False
    output = source.with_suffix(".ll")
    if marker == "__pcc_freestanding__":
        # The freestanding subset forbids private unexported functions;
        # retaining that explicit diagnostic is part of its machine contract.
        with pytest.raises(RuntimeError, match="freestanding module functions require @c_abi_export: private_helper"):
            compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                           backend="self", libpython_mode="off", ir_scaffold_mode="on")
        return
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    for symbol in ("user_manual_abi_private_helper", "manual_probe"):
        signature = re.search(r"^define [^\n]*@" + symbol + r"\([^\n]*", text, re.M)
        assert signature is not None
        assert signature.group(0).count("i64") == 2, signature.group(0)
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


def test_integer_abi_export_wire_agreement(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.pipeline_exports import (
        _read_native_exports_wire_for_module, _write_native_exports_wire,
    )

    source = tmp_path / "provider.py"
    source.write_text(CONSTRUCTORS.split("def main():", 1)[0] + PARAMETERS.split("def main():", 1)[0])
    _parsed, exports, derived = build_closed_world_context([str(source)], ["provider"])
    wire = tmp_path / "exports.wire"
    _write_native_exports_wire(str(wire), exports, derived)
    restored, _derived, _preload, _indexed = _read_native_exports_wire_for_module(str(wire), "consumer")
    for owner in (exports, restored):
        for name in ("check_uint", "produce"):
            assert owner["provider"][name]["box_int_abi"] is True
        for cls, method_name in (("Box", "__init__"), ("Row", "__init__"), ("Echo", "accept")):
            method = next(item for item in owner["provider"][cls]["methods"] if item["name"] == method_name)
            assert method["box_int_abi"] is True


def test_explicit_machine_int_constructor_abi_stays_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "machine_int_constructor.py"
    source.write_text('''from pcc.unsafe import null
from pcc import i64, u64
class Machine:
    def __init__(self, signed: i64, unsigned: u64):
        self.signed = signed
        self.unsigned = unsigned
def valueclass(cls):
    return cls
@valueclass
class Pair:
    first: int
    second: int
def main():
    machine = Machine(17, 25)
    pair = Pair(17, 25)
    assert pair.first + pair.second == 42
main()
''')
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    signature = re.search(r"^define [^\n]*@user_[^\n]*_Machine___init__\([^\n]*", text, re.M)
    assert signature is not None
    assert signature.group(0).count("i64") == 2, signature.group(0)
    assert "{ i64, i64 }" in text
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.parametrize("scaffold", [False, True])
def test_proven_bounded_list_abi_matches_export_wire(tmp_path, monkeypatch, scaffold):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.pipeline_exports import (
        _read_native_exports_wire_for_module, _write_native_exports_wire,
    )

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "bounded.py"
    source.write_text(("from pcc.unsafe import null\n" if scaffold else "") + '''def sum_ints(xs: list[int]) -> int:
    result: int = 0
    for item in xs:
        result = result + item
    return result
print(sum_ints([1, 2, 3, 4]))
''')
    _parsed, exports, derived = build_closed_world_context([str(source)], ["bounded"])
    wire = tmp_path / "bounded.wire"
    _write_native_exports_wire(str(wire), exports, derived)
    restored, _derived, _preload, _indexed = _read_native_exports_wire_for_module(str(wire), "consumer")
    assert exports["bounded"]["sum_ints"]["box_int_abi"] is False
    assert restored["bounded"]["sum_ints"]["box_int_abi"] is False
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    body = re.search(r"(?ms)^define [^\n]*@user_bounded_sum_ints\(.*?\n\}", text).group(0)
    assert re.search(r"define (?:external )?i64 @user_bounded_sum_ints\(ptr", body)
    assert "@py_list_get_i64_nonnegative" in body
    assert "@py_int_add" not in body
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.parametrize("name", PROGRAMS)
def test_ordinary_int_boundaries_reference(name):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAMS[name], {})
    assert output.getvalue() == "ORDINARY_INT_BOUNDARIES_OK\n"


@pytest.mark.parametrize("name", PROGRAMS)
def test_ordinary_int_boundaries_owned_ir(tmp_path, monkeypatch, name):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("ordinary_int_" + name + ".py")
    output = source.with_suffix(".ll")
    source.write_text(PROGRAMS[name])
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    if name == "constructors":
        for cls in ("Box", "Row"):
            signature = re.search(r"^define [^\n]*@user_[^\n]*_" + cls + r"___init__\([^\n]*", text, re.M)
            assert signature is not None
            assert "i64" not in signature.group(0), signature.group(0)
    elif name == "unpack":
        body = re.search(r"(?ms)^define [^\n]*@user_[^\n]*_main\(.*?\n\}", text)
        assert body is not None
        # Shift counts use the checked machine ABI. The only permitted unbox
        # here is the literal count 100 (tagged 201), never a tuple element.
        for operand in re.findall(r"@py_int_to_i64_lane\(ptr (%[^, ]+)", body.group(0)):
            assert re.search(re.escape(operand) + r" = inttoptr i64 201 to ptr", body.group(0))
        for local in ("left", "right", "value"):
            assert re.search(r"%" + local + r"\.addr\.\d+ = alloca ptr", body.group(0))
    else:
        for name in ("check_uint", "produce", "Echo_accept"):
            signature = re.search(r"^define [^\n]*@user_[^\n]*_" + name + r"\([^\n]*", text, re.M)
            assert signature is not None
            assert "i64" not in signature.group(0), signature.group(0)
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_ordinary_int_boundaries_native(tmp_path, monkeypatch, pcc_runtime_archive,
                                        python_program_compiler, name):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("ordinary_int_" + name + ".py")
    binary = source.with_suffix("")
    source.write_text(PROGRAMS[name])
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        (tmp_path / f"gc{backend}.stdout").write_text(ran.stdout)
        (tmp_path / f"gc{backend}.stderr").write_text(ran.stderr)
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout == "ORDINARY_INT_BOUNDARIES_OK\n"
