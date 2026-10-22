from __future__ import annotations

import hashlib

from pcc.frontends.python.module_action_dag import (
    ACTION_STAGES,
    GraphState,
    ModuleState,
    PublicSummary,
    action_key,
    load_action_artifact,
    load_graph_state_file,
    plan_actions,
    plan_from_payload,
    publish_action_artifact,
    publish_graph_state_file,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summary(name: str, version: str = "v1") -> PublicSummary:
    return PublicSummary.create(
        imported_types=("type:" + name,),
        exports=("export:" + name + ":" + version,),
        effects=("effect:pure",),
        layouts=("layout:" + name,),
    )


def _state(
    *,
    leaf_source: str = "leaf-v1",
    leaf_summary: str = "v1",
    compiler: str = "compiler-v1",
    runtime: str = "runtime-v1",
) -> GraphState:
    return GraphState.create(
        compiler_digest=_digest(compiler),
        runtime_abi_digest=_digest(runtime),
        target="arm64-apple-darwin",
        options_digest=_digest("options-v1"),
        modules=(
            ModuleState.create(
                "graph.leaf",
                _digest(leaf_source),
                (),
                _summary("leaf", leaf_summary),
            ),
            ModuleState.create(
                "graph.middle",
                _digest("middle-v1"),
                ("graph.leaf",),
                _summary("middle"),
            ),
            ModuleState.create(
                "graph.root",
                _digest("root-v1"),
                ("graph.middle",),
                _summary("root"),
            ),
        ),
    )


def _stages_by_module(plan):
    out = {}
    for action in plan.actions:
        out.setdefault(action.module, []).append(action.stage)
    return out


def test_noop_graph_compiles_zero_actions():
    state = _state()
    plan = plan_actions(state, state)
    assert plan.actions == ()
    assert not plan.full_rebuild
    assert plan.reason == "cache-hit"


def test_private_leaf_edit_rebuilds_only_that_module():
    old = _state()
    new = _state(leaf_source="leaf-private-edit")
    plan = plan_actions(old, new)
    assert _stages_by_module(plan) == {"graph.leaf": list(ACTION_STAGES)}
    assert not plan.full_rebuild


def test_public_leaf_edit_rebuilds_exact_reverse_closure():
    old = _state()
    new = _state(leaf_source="leaf-public-edit", leaf_summary="v2")
    plan = plan_actions(old, new)
    stages = _stages_by_module(plan)
    assert stages["graph.leaf"] == list(ACTION_STAGES)
    assert stages["graph.middle"] == [
        "module-ir",
        "transforms",
        "object-emission",
    ]
    assert stages["graph.root"] == stages["graph.middle"]


def test_compiler_or_runtime_abi_edit_rebuilds_every_action():
    old = _state()
    for new in (
        _state(compiler="compiler-v2"),
        _state(runtime="runtime-v2"),
    ):
        plan = plan_actions(old, new)
        assert plan.full_rebuild
        assert set(plan.modules()) == {
            "graph.leaf",
            "graph.middle",
            "graph.root",
        }
        assert len(plan.actions) == 3 * len(ACTION_STAGES)


def test_corrupt_or_unknown_summary_falls_back_to_full_rebuild():
    current = _state()
    payload = current.payload()
    payload["modules"][0]["summary"]["exports"] = ["tampered"]
    plan = plan_from_payload(payload, current)
    assert plan.full_rebuild
    assert plan.reason == "invalid-state"
    assert len(plan.actions) == 3 * len(ACTION_STAGES)


def test_action_key_binds_dependency_public_summary():
    old = _state()
    new = _state(leaf_source="leaf-public-edit", leaf_summary="v2")
    assert action_key(old, "graph.middle", "module-ir") != action_key(
        new,
        "graph.middle",
        "module-ir",
    )
    assert action_key(old, "graph.root", "module-ir") != action_key(
        new,
        "graph.root",
        "module-ir",
    )


def test_persisted_payload_round_trip_is_canonical():
    state = _state()
    plan = plan_from_payload(state.payload(), state)
    assert plan.actions == ()
    assert not plan.full_rebuild


def test_action_artifact_publish_round_trip_and_tamper_rejection(tmp_path):
    state = _state()
    plan = plan_actions(None, state)
    action = plan.actions[0]
    assert publish_action_artifact(str(tmp_path), action, b"owned-ir")
    assert load_action_artifact(str(tmp_path), action) == b"owned-ir"
    artifact = (
        tmp_path
        / "actions"
        / action.key[:2]
        / action.key
        / "artifact.bin"
    )
    artifact.write_bytes(b"tampered")
    assert load_action_artifact(str(tmp_path), action) is None


def test_graph_receipt_is_manifest_last_and_corruption_is_a_full_miss(tmp_path):
    state = _state()
    assert publish_graph_state_file(str(tmp_path), state)
    assert load_graph_state_file(str(tmp_path)) == state

    state_path = tmp_path / "state.json"
    state_path.write_text('{"schema":"wrong"}\n', encoding="utf-8")
    assert load_graph_state_file(str(tmp_path)) is None
