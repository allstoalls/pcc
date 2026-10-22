"""Host lowering of the exact COFF chunk helpers; no native execution claim."""

import ast
from pathlib import Path
import re

from pcc.backend import coff_x86_64 as coff
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _provider_source():
    source = Path(coff.__file__).read_text(encoding="utf-8")
    selected = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_COFF_CHUNK_BYTES"
            for target in node.targets
        ):
            selected.append(ast.get_source_segment(source, node))
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_append_coff_chunk", "_coff_section_payload",
        ):
            selected.append(ast.get_source_segment(source, node))
    assert len(selected) == 3
    return "from __future__ import annotations\n\n" + "\n\n".join(selected)


def test_coff_chunks_lower_to_owned_join_and_list_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider_name = "pcc.backend.coff_x86_64"
    consumer_name = "pcc.backend.coff_chunks_probe"
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text(_provider_source(), encoding="utf-8")
    consumer.write_text(
        "from pcc.backend.coff_x86_64 import _append_coff_chunk, _coff_section_payload\n"
        "def probe():\n"
        "    headers = bytearray(b'head')\n"
        "    chunks = [headers]\n"
        "    pending = []\n"
        "    data = _coff_section_payload(b'abc')\n"
        "    size = _append_coff_chunk(chunks, pending, 0, data)\n"
        "    data = _coff_section_payload(bytearray(b'x' * 8192))\n"
        "    size = _append_coff_chunk(chunks, pending, size, data)\n"
        "    size = _append_coff_chunk(chunks, pending, size, b'end')\n"
        "    if pending:\n"
        "        chunks.append(b''.join(pending))\n"
        "        pending.clear()\n"
        "    strings = [bytearray(b'\\0\\0\\0\\0'), b'name\\0']\n"
        "    chunks.extend(strings)\n"
        "    strings.clear()\n"
        "    return b''.join(chunks)\n",
        encoding="utf-8",
    )
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(consumer)], [provider_name, consumer_name],
    )
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
        verify_ir_text(text)
        assert "strict.nolib.stub" not in text
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
        # Only the compatibility snapshot extends a fresh empty bytearray.
        # Record accumulation and the consumer must not grow bytearrays.
        expected_extends = 1 if module.name == provider_name else 0
        assert len(re.findall(r"\bcall [^\n]*@py_bytearray_extend\(", text)) == expected_extends
        assert re.search(r"\bcall [^\n]*@py_bytes_join\(", text)
        assert re.search(r"\bcall [^\n]*@py_list_clear\(", text)
        texts[module.name] = text
    assert re.search(r"\bcall [^\n]*@py_list_extend\(", texts[consumer_name])
