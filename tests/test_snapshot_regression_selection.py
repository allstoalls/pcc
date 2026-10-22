"""A narrower affected scope must not silently retire reviewed regressions."""
from pathlib import Path

import pytest

from scripts.qualification.snapshot_selection import BASELINE, select_files


def test_previous_additions_and_mandatory_scopes_survive_narrow_selection():
    baseline = {"whole_files": ["tests/python/test_old.py"],
                "required_native": {"pcc": ["tests/python/test_async_await.py"]}}
    previous = {"whole_files": {
        "pcc": ["tests/python/test_previous.py"],
        "pcc-gateway": ["tests/test_previous_gateway.py"],
    }}
    selected = select_files(
        baseline, ["tests/test_gateway.py"],
        previous=previous, affected=[("pcc", "tests/python/test_new.py")],
    )
    assert selected["pcc"] == [
        "tests/python/test_async_await.py", "tests/python/test_new.py",
        "tests/python/test_old.py", "tests/python/test_previous.py",
    ]
    assert selected["pcc-gateway"] == [
        "tests/test_gateway.py", "tests/test_previous_gateway.py",
    ]


@pytest.mark.parametrize("path", [
    "tests/python/test_old.py::test_one", "../tests/test_old.py",
    "/tests/test_old.py", "tests/python", "pcc/test_old.py",
])
def test_affected_scope_cannot_replace_whole_files_with_nodes_or_other_paths(path):
    with pytest.raises(ValueError, match="whole test file"):
        select_files({"whole_files": [], "required_native": {"pcc": []}},
                     [], affected=[("pcc", path)])


def test_checked_baseline_keeps_reviewed_regression_families():
    import json

    baseline = json.loads(BASELINE.read_text())
    files = set(baseline["whole_files"])
    assert {
        "tests/python/test_async_await.py",
        "tests/python/test_imported_constructor_binding.py",
        "tests/python/test_imported_constructor_binding_native.py",
        "tests/python/test_virtual_thread_lowering_ownership.py",
        "tests/python/test_virtual_thread_park_effect.py",
        "tests/python/test_callable_metadata_ownership.py",
        "tests/python/test_builtin_exception_value_roots.py",
        "tests/python/test_slot_call_operand_roots.py",
        "tests/python/test_runtime_pointer_provenance.py",
    } <= files
    assert baseline["required_native"]["pcc-gateway"] == ["tests"]
    root = Path(__file__).resolve().parents[1]
    assert all((root / file).is_file() for file in files)


def test_plan_binds_test_bytes_and_never_claims_execution(tmp_path, monkeypatch):
    import hashlib
    import json

    from scripts.qualification import snapshot_selection

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({
        "whole_files": ["tests/test_core.py"],
        "required_native": {"pcc": [], "pcc-gateway": ["tests"]},
        "known_native_or_mixed_files": [],
    }))
    monkeypatch.setattr(snapshot_selection, "BASELINE", baseline)
    identities = {}
    roots = {}
    for repository, filename in (("pcc", "test_core.py"),
                                 ("pcc-gateway", "test_gateway.py")):
        root = tmp_path / repository
        (root / "tests").mkdir(parents=True)
        file = root / "tests" / filename
        file.write_text("def test_original(): pass\n")
        identities[repository] = {"tests/" + filename: {
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        }}
        roots[repository] = root
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps(identities))
    plan = snapshot_selection.make_plan(roots["pcc"], roots["pcc-gateway"], manifest)
    assert plan["execution_state"] == "UNRUN"
    assert plan["collection_is_execution"] is False
    (roots["pcc"] / "tests/test_core.py").write_text("def test_changed(): pass\n")
    with pytest.raises(ValueError, match="outside the frozen source"):
        snapshot_selection.make_plan(roots["pcc"], roots["pcc-gateway"], manifest)
