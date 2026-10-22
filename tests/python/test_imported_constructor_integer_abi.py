"""Imported-class constructors share the physical method-argument ABI."""
import re

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module


def _emit(module, exports, output):
    typed = infer_module(module, external_exports={key: value for key, value in exports.items() if key != module.name})
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    emitted = str(codegen.generate(typed))
    output.write_text(emitted)
    assert "define " in emitted
    assert emit_owned_object(emitted, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"
    return emitted


# Out-of-range values are not machine-i64 inputs for the runtime-port provider.
@pytest.mark.parametrize("value,machine_provider", [
    (value, machine_provider)
    for machine_provider in (False, True)
    for value in ("len(values)", "17", "18446744073709551615", "-(1 << 100)")
    if not (machine_provider and value in ("18446744073709551615", "-(1 << 100)"))
])
def test_imported_constructor_matches_provider_int_abi(tmp_path, monkeypatch, value, machine_provider):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    owner = "pcc.runtime.py.count_provider" if machine_provider else "provider"
    provider = tmp_path / "provider.py"
    provider.write_text(("__pcc_runtime_port__ = True\n" if machine_provider else "") + '''class Box:
    def __init__(self, count: int):
        self.count = count
''')
    entry = tmp_path / "entry.py"
    entry.write_text("from pcc.unsafe import null\nimport " + owner + " as provider\ndef main(values: list[str]):\n    return provider.Box(" + value + ")\n")
    modules, exports, _ = build_closed_world_context([str(provider), str(entry)], [owner, "entry"])
    method = next(item for item in exports[owner]["Box"]["methods"] if item["name"] == "__init__")
    assert method["box_int_abi"] is (not machine_provider)
    defining = _emit(modules[0], exports, tmp_path / "provider.ll")
    calling = _emit(modules[1], exports, tmp_path / "entry.ll")
    symbol = method["symbol"]
    for prefix, text in (("define", defining), ("declare", calling), ("call", calling)):
        matches = re.findall(r"(?:^|\s)" + prefix + r" [^\n]*@" + symbol + r"\(([^\n]*)\)", text, re.M)
        assert len(matches) == 1, (prefix, symbol)
        lane = matches[0].split(",")[1].strip().split()[0]
        assert lane == ("i64" if machine_provider else "ptr")
    assert "@pcc_gc_take_pinned_slot(" in calling
    assert not re.search(r"\bcall [^\n]*@py_cpy_", calling)


@pytest.mark.parametrize("call", ["provider.Box()", "provider.Box(count=len(values))", "provider.Child(len(values))", "provider.Varargs(len(values))"])
def test_imported_constructor_binding_paths_remain_valid(tmp_path, monkeypatch, call):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider = tmp_path / "provider.py"
    provider.write_text('''class Box:
    def __init__(self, count: int = 18446744073709551615):
        self.count = count
class Child(Box):
    pass
class Varargs:
    def __init__(self, count: int, *extra, **named):
        self.count = count
''')
    entry = tmp_path / "entry.py"
    entry.write_text("from pcc.unsafe import null\nimport provider\ndef main(values: list[str]):\n    return " + call + "\n")
    modules, exports, _ = build_closed_world_context([str(provider), str(entry)], ["provider", "entry"])
    for module in modules:
        text = _emit(module, exports, tmp_path / (module.name + ".ll"))
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
