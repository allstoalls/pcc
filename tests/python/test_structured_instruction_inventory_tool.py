from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "pcc_structured_instruction_inventory.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("structured_inventory_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fallback_opcode_inventory_counts_only_text_instructions() -> None:
    tool = _load_tool()
    lines = [
        ".section __DATA,__data,regular",
        "  add x0, x0, #1",
        ".section __TEXT,__text,regular,pure_instructions",
        ".globl _entry",
        "_entry:",
        "",
        "  add x0, x0, #1",
        "  cbz w0, L_done",
        "L_done:",
        "  ret",
        ".subsections_via_symbols",
    ]

    assert tool.fallback_opcodes(lines) == {"add": 1, "cbz": 1, "ret": 1}


def test_inventory_distinguishes_producer_words_from_text_reencoding(tmp_path, monkeypatch):
    import json
    from pcc.ir import ir
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    module = ir.Module(name="inventory-origin")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)
    function = ir.Function(module, ir.FunctionType(i64, [i64]), name="probe")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    # Bitwise operations still enter the late text encoder, even though its
    # result leaves no final assembler fallback. That is NOT zero projection.
    builder.ret(builder.and_(function.args[0], ir.Constant(i64, 7)))
    root = tmp_path / "inputs"
    root.mkdir()
    encode_indexed_module_file(str(root / "module_0.direct.pidx"), module.direct_indexed_module())

    output = tmp_path / "inventory.json"
    result = _load_tool().run(root, output)

    assert result["status"] == "COMPLETE"
    assert result["fallback"] == 0
    assert result["direct"] > 0
    assert result["text_encoded"] > 0
    assert result["text_opcodes"]["and"] >= 1
    assert result["direct"] + result["text_encoded"] == result["structured"]
    assert result["bootstrap_source_sha256"]
    assert json.loads(output.read_text()) == result
    assert result["available_files"] == result["selected_files"] == 1
    assert result["start_index"] == 0

    second = root / "module_1.direct.pidx"
    second.write_bytes((root / "module_0.direct.pidx").read_bytes())
    tail = _load_tool().run(root, tmp_path / "tail.json", start_index=1, limit=1)
    assert tail["available_files"] == 2
    assert tail["selected_files"] == 1
    assert tail["start_index"] == 1
    assert [row["name"] for row in tail["files"]] == ["module_1.direct.pidx"]
