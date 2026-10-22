from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from scripts import pcc_preload_compare as tool


@pytest.fixture
def comparison_inputs(tmp_path):
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.pipeline_exports import _write_native_exports_wire

    source = Path(type_infer.__file__).read_text(encoding="utf-8")
    selected = [
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in tool.PRELOAD_FUNCTIONS
    ]
    baseline = tmp_path / "baseline.py"
    baseline.write_text(
        "raise AssertionError('unselected module code executed')\n"
        + "\n\n".join(ast.get_source_segment(source, node) for node in selected),
        encoding="utf-8",
    )
    exports = {
        "z_owner": {"Widget": {
            "kind": "class", "class_name": "Widget", "owning_module": "z_owner",
            "field_names": ("value",), "field_types": (("value", ("int",)),),
            "methods": (), "base_names": (),
        }},
        "a_owner": {},
    }
    wire = tmp_path / "native_exports.json"
    _write_native_exports_wire(str(wire), exports, {})
    return baseline, wire, tmp_path / "receipt.json", exports


def test_real_wire_exact_index_links_baseline_functions_and_retains_order(comparison_inputs):
    from pcc.frontends.python import type_infer

    baseline, wire, output, exports = comparison_inputs
    expected = type_infer.build_unique_external_class_preload_index(exports)
    result = tool.run(baseline, wire, output)
    expected_bytes = json.dumps(expected, separators=(",", ":")).encode()
    assert result["status"] == "EXACT"
    assert result["semantic_equal"] and result["ordered_bytes_equal"]
    assert result["modules"] == 2
    assert result["roots"] == 2 and result["nonempty_roots"] == 1
    assert result["types"] == 1 and result["base_keys"] == 2
    assert result["index_sha256"] == hashlib.sha256(expected_bytes).hexdigest()
    assert result["index_bytes"] == len(expected_bytes)
    assert result["baseline_source_sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert result["exports_sha256"] == hashlib.sha256(wire.read_bytes()).hexdigest()
    assert json.loads(output.read_text()) == result


def test_baseline_extraction_preserves_globals_and_links_both_replacements():
    source = '''
raise AssertionError("do not execute unrelated module statements")
def build_unique_external_class_preload(exports):
    return {"types": seed, "base_keys": (), "roots": {}}
def build_unique_external_class_preload_index(exports):
    return build_unique_external_class_preload(exports)
'''
    original_globals = {
        "seed": (("class", "Known"),),
        "build_unique_external_class_preload": lambda _: pytest.fail("current preload leaked into baseline"),
    }
    baseline = tool.extract_baseline(source, "baseline.py", original_globals)
    assert baseline[tool.PRELOAD_FUNCTIONS[1]]({})["types"] is original_globals["seed"]
    assert original_globals["build_unique_external_class_preload"] is not baseline[tool.PRELOAD_FUNCTIONS[0]]


@pytest.mark.parametrize("order_only", [False, True])
def test_difference_denies_semantic_changes_and_equal_dict_order_changes(
    comparison_inputs, monkeypatch, capsys, order_only,
):
    from pcc.frontends.python import type_infer

    baseline, wire, output, _exports = comparison_inputs
    current = type_infer.build_unique_external_class_preload_index

    def changed(exports):
        result = current(exports)
        if order_only:
            result["roots"] = dict(reversed(tuple(result["roots"].items())))
        else:
            result["base_keys"] = ()
        return result

    monkeypatch.setattr(type_infer, "build_unique_external_class_preload_index", changed)
    assert tool.main([
        "--baseline-source", str(baseline), "--exports-wire", str(wire), "--out", str(output),
    ]) == 2
    assert "DIFFERENT" in capsys.readouterr().out
    receipt = json.loads(output.read_text())
    assert receipt["semantic_equal"] is order_only
    assert not receipt["ordered_bytes_equal"]
    assert receipt["index_sha256"] != receipt["baseline_index_sha256"]


@pytest.mark.parametrize("name", tool.PRELOAD_FUNCTIONS)
@pytest.mark.parametrize("copies", [0, 2])
def test_baseline_missing_or_duplicate_definition_publishes_nothing(
    comparison_inputs, name, copies,
):
    baseline, wire, output, _exports = comparison_inputs
    source = ""
    for function_name in tool.PRELOAD_FUNCTIONS:
        count = copies if function_name == name else 1
        source += (f"def {function_name}(exports):\n    return {{}}\n" * count)
    baseline.write_text(source)
    with pytest.raises(tool.PreloadCompareError, match="exactly one " + name):
        tool.run(baseline, wire, output)
    assert not output.exists()


@pytest.mark.parametrize("target", ["baseline", "wire"])
def test_input_drift_rejects_comparison_without_receipt(comparison_inputs, monkeypatch, target):
    from pcc.frontends.python import type_infer

    baseline, wire, output, _exports = comparison_inputs
    changed_path = baseline if target == "baseline" else wire
    current = type_infer.build_unique_external_class_preload_index

    def drifting(exports):
        result = current(exports)
        with changed_path.open("a") as stream:
            stream.write("\n")
        return result

    monkeypatch.setattr(type_infer, "build_unique_external_class_preload_index", drifting)
    with pytest.raises(tool.PreloadCompareError, match="changed during comparison"):
        tool.run(baseline, wire, output)
    assert not output.exists()


def test_candidate_source_drift_is_checked(tmp_path):
    candidate = tmp_path / "type_infer.py"
    candidate.write_text("original\n")
    hashes = {"candidate_source": tool._sha256(candidate)}
    candidate.write_text("changed\n")
    with pytest.raises(tool.PreloadCompareError, match="candidate_source changed"):
        tool._require_unchanged({"candidate_source": candidate}, hashes)


def test_existing_receipt_is_preserved_before_loading_inputs(comparison_inputs):
    baseline, wire, output, _exports = comparison_inputs
    output.write_bytes(b"keep this exact receipt\n")
    baseline.write_text("not valid Python")
    with pytest.raises(tool.PreloadCompareError, match="refusing existing output"):
        tool.run(baseline, wire, output)
    assert output.read_bytes() == b"keep this exact receipt\n"


def test_receipt_created_during_comparison_is_not_overwritten(comparison_inputs, monkeypatch):
    from pcc.frontends.python import type_infer

    baseline, wire, output, _exports = comparison_inputs
    current = type_infer.build_unique_external_class_preload_index

    def racing(exports):
        result = current(exports)
        output.write_text("another owner's receipt")
        return result

    monkeypatch.setattr(type_infer, "build_unique_external_class_preload_index", racing)
    with pytest.raises(FileExistsError):
        tool.run(baseline, wire, output)
    assert output.read_text() == "another owner's receipt"
