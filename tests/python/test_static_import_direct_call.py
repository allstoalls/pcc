"""Calls through a live ``from m import f`` binding reach ``f`` directly.

A module-level imported name is a live binding owned by the module
namespace, so its calls went through the generic callable protocol: a module
attribute lookup, an argument tuple, a merged keyword dict and
``py_obj_call_slots`` before the provider's adapter unpacked them again.
That protocol was about a third of a pcc1 worker in the x86 assembler's
``encode_instruction`` calls. The callee is still read first, as Python
requires, but while it is the provider's plain function object the already
evaluated arguments feed its native body; a rebound name takes the dynamic
call with the same argument values.
"""

import os
import re
import subprocess
import sys

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module


PROVIDER = '''def enc(line, *, pc, labels=None, section="text"):
    extra = 0 if labels is None else len(labels)
    return len(line) + pc + extra + len(section)


def pos(line, pc):
    return len(line) + pc


def other(line, pc):
    return -pc


def describe(value):
    return "value=" + str(value)


def flag(value: int) -> bool:
    return value > 2


def half(value: int) -> float:
    return value / 2


def note(items: list) -> None:
    items.append(1)


def fail(value):
    raise ValueError("bad " + str(value))
'''

MAIN = '''from sicall_prov import describe, enc, fail, flag, half, note, other, pos


def use_kw(line):
    return enc(line, pc=3, labels={"a": 1})


def use_default(line):
    return enc(line, pc=2)


def use_pos(line):
    return pos(line, 3)


def rebind_then(value):
    globals()["pos"] = other
    return value


def gen():
    yield pos("abc", 1)


def main():
    total = 0
    index = 0
    while index < 1000:
        total += use_kw("ab") + use_default("x") + use_pos("abc")
        index += 1
    print(total)
    print(describe(total))
    print(flag(3), flag(1), half(5))
    items = []
    note(items)
    print(items, note(items))
    try:
        fail(4)
    except ValueError as exc:
        print("caught", exc)
    print(list(gen()))
    print(pos("ab", rebind_then(2)))
    print(use_pos("abc"))
    print(use_kw("ab"), use_default("x"))
    del globals()["pos"]
    try:
        use_pos("a")
    except NameError:
        print("unbound")


main()
'''

# Scalar and void results are boxed; an argument that rebinds the name
# leaves the callee evaluated before it; a generator body keeps the
# dynamic call; a deleted binding raises NameError.
EXPECTED = "23000\nvalue=23000\nTrue False 2.5\n[1, 1] None\ncaught bad 4\n[4]\n4\n-3\n10 7\nunbound\n"


def _write(tmp_path):
    (tmp_path / "sicall_prov.py").write_text(PROVIDER)
    main = tmp_path / "sicall_main.py"
    main.write_text(MAIN)
    return main


def _function_body(text, suffix):
    match = re.search(r"^define [^\n]*@(user_\w*" + suffix + r")\(.*?^}", text, re.M | re.S)
    assert match, suffix
    return match.group(0)


def _main_ir(tmp_path):
    _write(tmp_path)
    paths = [str(tmp_path / "sicall_prov.py"), str(tmp_path / "sicall_main.py")]
    modules, exports, derived = build_closed_world_context(paths, ["sicall_prov", "sicall_main"])
    module = next(module for module in modules if module.name == "sicall_main")
    external = {name: value for name, value in exports.items() if name != module.name}
    typed = infer_module(module, external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._native_module_exports = exports
    codegen._sibling_module_inits = tuple(external)
    return str(codegen.generate(typed))


def test_live_import_calls_guard_a_direct_native_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    text = _main_ir(tmp_path)
    for function, callee in (("use_kw", "enc"), ("use_default", "enc"), ("use_pos", "pos")):
        body = _function_body(text, function)
        assert "@py_func_slot_has_native_entry(" in body, function
        assert re.search(r"call [^\n]*@user_sicall_prov_" + callee + r"\(", body), function
        # The rebound name still has its dynamic call.
        assert "@py_obj_call_slots(" in body, function
        assert "@user_sicall_prov_" + callee + "_native_adapter" in body, function


def test_cpython_reference_output(tmp_path):
    main = _write(tmp_path)
    result = subprocess.run([sys.executable, str(main)], cwd=tmp_path, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED


@pytest.mark.integration
def test_live_import_calls_native(tmp_path, pcc_runtime_archive):
    main = _write(tmp_path)
    output = tmp_path / "sicall_main"
    compile_python(str(main), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert (result.returncode, result.stdout, result.stderr) == (
            0, EXPECTED, ""), gc


STOPPED_PROVIDER = '''import sicall_missing_module


def stopped(value):
    return value + 1
'''

STOPPED_MAIN = '''from sicall_stopped import stopped


def use(value):
    return stopped(value)
'''


def test_exported_function_adapter_exists_when_module_execution_stops_early(tmp_path, monkeypatch):
    # Importers may reference the adapter even though this module's top
    # raises ImportError before the def runs; the link must still succeed.
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    from pcc.frontends.python.pipeline_closed_world import _closed_world_function_object_exports

    (tmp_path / "sicall_stopped.py").write_text(STOPPED_PROVIDER)
    (tmp_path / "sicall_stopped_main.py").write_text(STOPPED_MAIN)
    paths = [str(tmp_path / "sicall_stopped.py"), str(tmp_path / "sicall_stopped_main.py")]
    names = ["sicall_stopped", "sicall_stopped_main"]
    modules, exports, derived = build_closed_world_context(paths, names)
    assert exports["sicall_stopped"]["stopped"]["needs_object"]
    module = next(module for module in modules if module.name == "sicall_stopped")
    external = {name: value for name, value in exports.items() if name != module.name}
    typed = infer_module(module, external_exports=external, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._skip_program_main = True
    codegen._native_module_exports = external
    codegen._sibling_module_inits = tuple(external)
    codegen._native_function_object_exports = _closed_world_function_object_exports(exports, module.name)
    text = str(codegen.generate(typed))
    top = re.search(r"^define [^\n]*@_pcc_py_module_top_sicall_stopped\(.*?^}", text, re.M | re.S).group(0)
    assert "@py_exc_new(i64 20," in top and "pcc.def.binding.publish" not in top
    assert re.search(r"^define [^\n]*@user_sicall_stopped_stopped_native_adapter\(", text, re.M)
