"""Lower the exact section builder across an imported native class boundary."""

import ast
from pathlib import Path
import re

from pcc.backend import x86_64_asm_driver as assembler
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _provider_source():
    source = Path(assembler.__file__).read_text(encoding="utf-8")
    selected = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_SECTION_PAYLOAD_CHUNK_BYTES"
            for target in node.targets
        ):
            selected.append(ast.get_source_segment(source, node))
        if isinstance(node, ast.ClassDef) and node.name == "_SectionPayload":
            selected.append(ast.get_source_segment(source, node))
    assert len(selected) == 2
    return "from __future__ import annotations\n\n" + "\n\n".join(selected)


def test_section_payload_lowers_exact_fields_methods_and_cross_module_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider_name = "pcc.backend.x86_64_asm_driver"
    consumer_name = "pcc.backend.section_payload_probe"
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text(_provider_source(), encoding="utf-8")
    consumer.write_text(
        "from pcc.backend.x86_64_asm_driver import _SectionPayload\n"
        "def probe():\n"
        "    payload = _SectionPayload()\n"
        "    payload.append(b'abc')\n"
        "    payload.append(b'def')\n"
        "    return payload.finish() == b'abcdef'\n",
        encoding="utf-8",
    )
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(consumer)], [provider_name, consumer_name],
    )
    payload = exports[provider_name]["_SectionPayload"]
    assert payload["field_names"] == ("chunks", "pending", "pending_size")
    fields = dict(payload["field_types"])
    assert fields["chunks"] == fields["pending"] == ("list", ("bytes",))
    methods = {method["name"]: method for method in payload["methods"]}
    assert set(methods) == {"__init__", "_flush", "append", "finish"}
    texts = {}
    for module in modules:
        external = {name: value for name, value in exports.items() if name != module.name}
        typed = infer_module(module, external_exports=external, derived_class_map=derived)
        codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        codegen._sibling_module_inits = tuple(external)
        text = str(codegen.generate(typed))
        (tmp_path / (module.name + ".ll")).write_text(text, encoding="utf-8")
        verify_ir_text(text)
        assert "strict.nolib.stub" not in text
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
        texts[module.name] = text
    defining = texts[provider_name]
    for method in methods.values():
        assert re.search(r"^define [^\n]*@" + re.escape(method["symbol"]) + r"\(", defining, re.M)
    assert re.search(r"\bcall [^\n]*@py_bytes_join\(", defining)
    assert re.search(r"\bcall [^\n]*@py_list_clear\(", defining)
    assert not re.search(r"\bcall [^\n]*@py_bytearray_extend\(", defining)
    for name in ("append", "finish"):
        assert "@" + methods[name]["symbol"] + "(" in texts[consumer_name]
