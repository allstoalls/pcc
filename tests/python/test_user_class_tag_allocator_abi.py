"""The public allocator ABI preserves signed 32-bit tags and exhaustion."""
import ast
from pathlib import Path
import re

import pytest

from pcc.frontends.python.owned_runtime_build import _compile_runtime_module
from tests.owned_ir_validation import verify_ir_text


ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "py_subs_alloc_user_tag"
TARGET = "x86_64-unknown-linux-gnu"


def test_header_export_and_only_production_caller_agree():
    header = (ROOT / "pcc/runtime/include/py_runtime.h").read_text()
    assert re.search(r"\bint32_t\s+" + SYMBOL + r"\(void\);", header)
    substrate = ast.parse((ROOT / "pcc/runtime/py/py_substrate.py").read_text())
    definition = next(node for node in substrate.body
                      if isinstance(node, ast.FunctionDef) and node.name == SYMBOL)
    decorator, = definition.decorator_list
    assert isinstance(decorator, ast.Call)
    assert isinstance(decorator.func, ast.Name)
    assert decorator.func.id == "c_abi_typed_export"
    assert [ast.literal_eval(arg) for arg in decorator.args] == [SYMBOL, "i32", ()]
    callers = []
    for path in sorted((ROOT / "pcc/runtime/py").glob("*.py")):
        text = path.read_text()
        if SYMBOL not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "extern" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == SYMBOL):
                callers.append((path.name, ast.unparse(node.args[1]), ast.unparse(node.args[2])))
    assert callers == [("py_class.py", "()", "c_int32")]


def assert_allocator_component_abi(name, text):
    verify_ir_text(text)
    if name == "py_substrate":
        match = re.search(r"^define[^\n]*\bi32 @" + SYMBOL + r"\(\)[^\n{]*\{\n(.*?)^}", text, re.M | re.S)
        assert match, "public allocator must implement the header's i32 return"
        body = match.group(1)
        assert "cmpxchg" in body and "load atomic i32" in body
        assert re.search(r"ret i32 -1\b", body) or re.search(r"trunc i64 -1 to i32", body)
        assert re.search(r"trunc i64 %[-\w.]+ to i32", body)
    else:
        match = re.search(r"^define[^\n]*@user_py_class__alloc_user_tag\(\)[^\n{]*\{\n(.*?)^}", text, re.M | re.S)
        assert match
        body = match.group(1)
        call = re.search(r"(%[-\w.]+) = call i32(?: \(\))? @" + SYMBOL + r"\(\)", body)
        assert call, "private class helper must call the actual i32 ABI"
        extension = re.search(r"(%[-\w.]+) = sext i32 " + re.escape(call.group(1)) + r" to i64", body)
        assert extension, "-1 exhaustion must remain signed in the runtime int lane"
        assert re.search(r"ret i64 " + re.escape(extension.group(1)) + r"\b", body)


@pytest.mark.parametrize("name", ["py_substrate", "py_class"])
def test_actual_runtime_component_preserves_allocator_abi(tmp_path, name):
    output = tmp_path / (name + ".ll")
    _compile_runtime_module(name, str(ROOT / "pcc/runtime/py" / (name + ".py")),
                            str(output), TARGET)
    assert_allocator_component_abi(name, output.read_text())
