"""``cls(...)`` constructs the lexical class when nothing can subclass it.

A classmethod's ``cls`` is the receiver's runtime class, so construction
normally goes through the runtime callable binder (an argument tuple, a
keyword dict and ``py_obj_call_slots``). When the closed-world class graph
has no subclass of the owner, ``cls`` can only be the owner itself and the
call is an ordinary constructor call. ``Instruction.from_text`` in the IR
mutator spent a fifth of a pcc1 pass phase in that binder. A compile with
no closed world (one module, no export graph) keeps the runtime binder.
"""

import os
import re
import subprocess

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module


SHAPES = '''class Final:
    def __init__(self, text, size=0):
        self.text = text
        self.size = size

    @classmethod
    def parse(cls, text):
        return cls(text=text, size=len(text))


class Base:
    def __init__(self, value):
        self.value = value

    @classmethod
    def make(cls, value):
        return cls(value)
'''

MAIN = '''from clsm_shapes import Base, Final


class Child(Base):
    pass


def main():
    item = Final.parse("abc")
    assert type(item) is Final and item.text == "abc" and item.size == 3
    child = Child.make(4)
    assert type(child) is Child and child.value == 4
    assert type(Base.make(5)) is Base
    print("CLASSMETHOD_FINAL_OK")


main()
'''


def _write(tmp_path):
    (tmp_path / "clsm_shapes.py").write_text(SHAPES)
    main = tmp_path / "clsm_main.py"
    main.write_text(MAIN)
    return main


def _function_body(text, suffix):
    match = re.search(r"^define [^\n]*@(user_\w*" + suffix + r")\(.*?^}", text, re.M | re.S)
    assert match, suffix
    return match.group(0)


def _shapes_ir(tmp_path, closed_world):
    _write(tmp_path)
    paths = [str(tmp_path / "clsm_shapes.py"), str(tmp_path / "clsm_main.py")]
    modules, exports, derived = build_closed_world_context(paths, ["clsm_shapes", "clsm_main"])
    module = next(module for module in modules if module.name == "clsm_shapes")
    external = {name: value for name, value in exports.items() if name != module.name}
    typed = infer_module(module, external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    if closed_world:
        codegen._native_module_exports = exports
    return str(codegen.generate(typed))


def test_final_class_constructs_directly_and_subclassed_class_binds_at_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    text = _shapes_ir(tmp_path, closed_world=True)
    parse = _function_body(text, "Final_parse")
    assert "ctor.unpack" not in parse
    assert not re.search(r"@py_obj_call_slots\(", parse)
    # Child, in the other module, derives from Base.
    make = _function_body(text, "Base_make")
    assert "ctor.unpack.call" in make
    assert re.search(r"@py_obj_call_slots\(", make)


def test_without_an_export_graph_every_classmethod_binds_at_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    text = _shapes_ir(tmp_path, closed_world=False)
    for suffix in ("Final_parse", "Base_make"):
        assert "ctor.unpack.call" in _function_body(text, suffix)


@pytest.mark.integration
def test_classmethod_constructors_native(tmp_path, pcc_runtime_archive):
    main = _write(tmp_path)
    output = tmp_path / "clsm_main"
    compile_python(str(main), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "CLASSMETHOD_FINAL_OK\n", ""), gc
