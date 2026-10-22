"""The migration observer must preserve emission while exposing false matches."""

from copy import deepcopy

import pytest

from pcc.frontends.python.codegen.call_identity import (
    ClassIdentity,
    MethodIdentity,
    ReceiverIdentity,
    SymbolIdentity,
    resolve_method_identity,
)
from pcc.tools.ir_scaffold_observer import (
    CodegenIdentityResolver,
    ScaffoldDecisionRecorder,
    compare_decision_records,
    observe_scaffold_decisions,
)


def _compile(tmp_path, source, output):
    from pcc.frontends.python.pipeline import compile_python

    path = tmp_path / "source.py"
    path.write_text(source, encoding="utf-8")
    dest = tmp_path / output
    compile_python(str(path), str(dest), emit_llvm_only=True, ir_scaffold_mode="on")
    return dest.read_text(encoding="utf-8")


def test_observer_does_not_change_emitted_ir(tmp_path):
    source = (
        "from pcc.ir.compat import ir\n"
        "def use_method(a, b):\n"
        "    builder = ir.IRBuilder()\n"
        "    return builder.add(a, b)\n"
    )
    before = _compile(tmp_path, source, "before.ll")
    recorder = ScaffoldDecisionRecorder()
    with observe_scaffold_decisions(recorder):
        after = _compile(tmp_path, source, "after.ll")
    assert before == after
    calls = [row for row in recorder.records if row["kind"] == "method"]
    assert len(calls) == 1
    row = calls[0]
    assert row["member"] == "add"
    assert row["legacy_callees"][0]["symbol"] == "user_pcc_ir_ir_IRBuilder_add"
    assert len(row["legacy_callees"][0]["parameter_types"]) == 4
    assert row["resolution"] == "unknown"
    assert not row["same_emitted_target"]


def test_nested_calls_are_attributed_to_their_own_receiver(tmp_path):
    source = (
        "from pcc.ir.compat import ir\n"
        "def use_method(a, b):\n"
        "    builder = ir.IRBuilder()\n"
        "    return builder.add(builder.mul(a, b), b)\n"
    )
    recorder = ScaffoldDecisionRecorder()
    with observe_scaffold_decisions(recorder):
        _compile(tmp_path, source, "nested.ll")
    rows = [row for row in recorder.records if row["kind"] == "method"]
    assert {row["member"] for row in rows} == {"add", "mul"}
    for row in rows:
        assert [callee["symbol"] for callee in row["legacy_callees"]] == [
            "user_pcc_ir_ir_IRBuilder_" + row["member"]
        ]


def test_provider_class_value_is_recorded_without_a_call(tmp_path):
    recorder = ScaffoldDecisionRecorder()
    with observe_scaffold_decisions(recorder):
        _compile(
            tmp_path,
            "from pcc.ir.compat import ir\n"
            "def class_value():\n    return ir.IRBuilder\n",
            "class-value.ll",
        )
    rows = [row for row in recorder.records if row["kind"] == "symbol-value"]
    assert len(rows) == 1
    assert rows[0]["legacy_value_globals"] == [".class.pcc_ir_ir.IRBuilder"]
    assert rows[0]["legacy_callees"] == []


def test_observer_reports_user_builder_false_match_without_changing_dispatch(tmp_path):
    # Facts come from a verified call site, not from the name 'builder'. The
    # syntactic matcher still sees the spelling, but emission now rejects this
    # receiver, so the observer sees no IR provider call.
    owner = SymbolIdentity("application", "TextBuilder")
    method = MethodIdentity(
        SymbolIdentity("application", "TextBuilder.add"),
        "user_application_TextBuilder_add",
    )
    classes = {owner: ClassIdentity(owner, (owner,), {"add": method})}
    fact = ReceiverIdentity(
        (owner,), True, True, False, ("test:all-calls-and-writes-checked",)
    )
    recorder = ScaffoldDecisionRecorder(
        lambda _host, _expr: resolve_method_identity(fact, "add", classes)
    )
    with observe_scaffold_decisions(recorder):
        _compile(
            tmp_path,
            "def render(builder, a, b):\n    return builder.add(a, b)\n",
            "wrong-owner.ll",
        )
    row = recorder.records[0]
    assert row["target"] == "application:TextBuilder.add"
    assert row["legacy_callees"] == []
    assert not row["same_emitted_target"]
    assert (
        compare_decision_records(recorder.records, recorder.records)[0]["change"]
        == "different-target-or-adapter"
    )


def test_verified_flow_uses_the_real_user_class_declaration(tmp_path):
    # Flow contracts supply only the receiver fact; they do not duplicate a
    # symbol/ABI table. ClassInfo supplies the actual method declaration.
    source = (
        "class TextBuilder:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n"
        "def render() -> int:\n"
        "    builder = TextBuilder()\n"
        "    return builder.add(2, 3)\n"
    )
    def verified_receiver(host, receiver):
        if getattr(receiver, "ident", None) != "builder":
            return None
        return ReceiverIdentity(
            (SymbolIdentity(host.ast_module.name, "TextBuilder"),),
            complete=True,
            lookup_stable=True,
            evidence=("test:render-exact-constructor-and-no-mutations",),
        )

    before = _compile(tmp_path, source, "declared-before.ll")
    recorder = ScaffoldDecisionRecorder(CodegenIdentityResolver(verified_receiver))
    with observe_scaffold_decisions(recorder):
        after = _compile(tmp_path, source, "declared-after.ll")
    assert before == after
    row = next(row for row in recorder.records if row["member"] == "add")
    assert row["resolution"] == "proven", row["reason"]
    assert row["target"].endswith(":TextBuilder.add")
    assert row["proposed_emitted_symbol"].endswith("_TextBuilder_add")
    assert row["proposed_abi"] is not None
    assert not row["same_emitted_target"]
    assert not row["same_declared_abi"]


def test_observer_restores_methods_after_lowering_failure(tmp_path):
    from pcc.frontends.python.codegen.ir_scaffold_lowering import IrScaffoldLoweringMixin

    original = IrScaffoldLoweringMixin._maybe_emit_ir_scaffold_call
    recorder = ScaffoldDecisionRecorder()
    with pytest.raises(Exception):
        with observe_scaffold_decisions(recorder):
            _compile(
                tmp_path,
                "from pcc.ir.compat import ir\n"
                "def broken():\n"
                "    builder = ir.IRBuilder()\n"
                "    return builder.add()\n",
                "bad.ll",
            )
    assert IrScaffoldLoweringMixin._maybe_emit_ir_scaffold_call is original
    assert recorder.records and not recorder.records[-1]["completed"]


def test_comparison_does_not_accept_equal_counts_with_different_abi():
    row = {
        "module": "module",
        "function": "f",
        "line": 3,
        "column": 4,
        "occurrence": 0,
        "kind": "method",
        "member": "load",
        "completed": True,
        "resolution": "proven",
        "reason": "checked",
        "same_emitted_target": True,
        "same_declared_abi": True,
        "target": "provider:IRBuilder.load",
        "proposed_emitted_symbol": "callee",
        "legacy_callees": [{"symbol": "callee", "parameter_types": ["ptr", "i64"]}],
    }
    after = deepcopy(row)
    after["legacy_callees"][0]["parameter_types"][1] = "ptr"
    assert compare_decision_records([row], [after])[0]["change"] == "legacy_callees"
    assert compare_decision_records([row], [])[0]["change"] == "removed"


def test_matching_symbol_does_not_prove_the_declared_abi():
    row = {
        "module": "m",
        "function": "f",
        "line": 1,
        "column": 0,
        "occurrence": 0,
        "kind": "method",
        "member": "load",
        "completed": True,
        "resolution": "proven",
        "reason": "checked",
        "same_emitted_target": True,
        "same_declared_abi": False,
        "target": "provider:Builder.load",
        "legacy_callees": [],
    }
    assert compare_decision_records([row], [row]) == [
        {
            "point": ["m", "f", 1, 0, 0, "method", "load"],
            "change": "declared-abi-not-proved",
        }
    ]
