"""typing.cast preserves object identity and carries a valid result owner."""
import os
import subprocess

import pytest


@pytest.mark.parametrize("raw_scaffold", [False, True])
@pytest.mark.parametrize("spelling", ["cast", "typing.cast"])
def test_cast_of_dynamic_tuple_element_preserves_container_owners(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
    raw_scaffold, spelling,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "cast_tuple_owner.py"
    source.write_text('''from typing import cast
import typing
RAW_IMPORT
import gc
freed = 0
class Item:
    def __init__(self, value: int):
        self.value = value
    def __del__(self):
        global freed
        freed += 1
def visit(items: tuple):
    for element in items:
        item: Item = CAST(Item, element)
        assert item.value > 0
def main():
    items = (Item(41), Item(42))
    visit(items)
    assert freed == 0
    gc.collect()
    assert freed == 0
    assert items[0].value == 41
    assert items[1].value == 42
    visit(items)
    assert freed == 0
    items = None
    gc.collect()
    assert freed == 2
    print("cast-tuple-owner-ok")
main()
'''.replace("RAW_IMPORT", "from pcc.unsafe import ptr_is_null" if raw_scaffold else "")
       .replace("CAST", spelling))
    binary = tmp_path / "cast_tuple_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "cast-tuple-owner-ok"


@pytest.mark.parametrize("source_kind", ["borrowed", "fresh", "attribute"])
@pytest.mark.parametrize("spelling", ["cast", "typing.cast"])
def test_typing_cast_keeps_its_alias_alive(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
    source_kind, spelling,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    setup, operand, release = {
        "borrowed": ("original = make()", "original", "original = None"),
        "fresh": ("pass", "make()", "pass"),
        "attribute": ("original = Holder(make())", "original.value", "original = None"),
    }[source_kind]
    source = tmp_path / "cast_owner.py"
    source.write_text('''from typing import cast
import typing
import gc
destroyed = 0
class Item:
    def __init__(self):
        self.value = 42
    def __del__(self):
        global destroyed
        destroyed += 1
        gc.collect()
class Holder:
    def __init__(self, value):
        self.value = value
def make() -> Item:
    return Item()
def main():
    SETUP
    alias = CAST(Item, OPERAND)
    RELEASE
    gc.collect()
    assert destroyed == 0
    assert alias.value == 42
    alias.value = 43
    assert alias.value == 43
    alias = None
    gc.collect()
    gc.collect()
    assert destroyed == 1
    print("cast-owner-ok")
main()
'''.replace("SETUP", setup).replace("CAST", spelling).replace("OPERAND", operand).replace("RELEASE", release))
    binary = tmp_path / "cast_owner"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                             env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.strip() == "cast-owner-ok"


@pytest.mark.parametrize("freestanding", [False, True])
def test_typing_cast_preserves_raw_pointer_domain(tmp_path, monkeypatch, freestanding):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "cast_raw.py"
    source.write_text('''from typing import cast
from pcc.extern import c_abi_export, c_ptr
FLAG
@c_abi_export("cast_raw")
def cast_raw(value: c_ptr) -> c_ptr:
    return cast(c_ptr, value)
'''.replace("FLAG", "__pcc_freestanding__ = True" if freestanding else ""))
    output = tmp_path / "cast_raw.ll"
    if freestanding:
        # This module domain rejects typing imports before code generation.
        # Preserve that boundary rather than treating an uncompiled body as
        # evidence that its pointers never reached the managed runtime.
        with pytest.raises(RuntimeError, match="freestanding modules only support imports.*typing"):
            compile_python(str(source), str(output), backend="self", libpython_mode="off",
                           emit_llvm_only=True, python_library=True)
        return
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    start = text.index("define", text.index("@cast_raw") - 32)
    body = text[start:text.index("\n}", start)]
    assert "@pcc_gc_retain" not in body
    assert "@pcc_gc_pin" not in body
    assert "@py_incref" not in body
