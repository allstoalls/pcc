"""Diagnostic outlining keeps reader results and exception boundaries intact."""

import ast
import copy
import inspect
from types import SimpleNamespace

import pytest

from pcc.frontends.python import worker_resource_plan as policy


def _inline_reference():
    """Undo exactly the helper extraction; the frozen receipt checks 7bf AST."""
    reader = ast.parse(inspect.getsource(policy.read_tree_state)).body[0]
    helper = ast.parse(inspect.getsource(policy._set_tree_state_diagnostic)).body[0]
    final = reader.body[-1].finalbody[0].body[0]
    assert isinstance(final, ast.Try)
    call = final.body[0].value
    assert isinstance(call, ast.Call) and call.func.id == "_set_tree_state_diagnostic"
    assert [arg.id for arg in call.args] == [arg.arg for arg in helper.args.args]
    assert call.keywords == [] and len(final.body) == 1
    final.body = copy.deepcopy(helper.body)
    tree = ast.fix_missing_locations(ast.Module(body=[reader], type_ignores=[]))
    namespace = dict(policy.__dict__)
    exec(compile(tree, "inline-worker-state-reference.py", "exec"), namespace)
    return namespace["read_tree_state"], namespace, tree


def _state():
    return policy.TREE_STATE_SCHEMA + "\n100.0\n1000\n10\t0\t50\n11\t10\t100\n12\t11\t200\n13\t12\t30\n"


@pytest.mark.parametrize("case", (
    "accepted", "inclusive", "empty", "missing", "header", "timestamp", "age",
    "future", "barrier", "budget", "fields", "integer", "duplicate", "owner",
    "owner-rss", "active-owner", "active-missing", "active-parent", "active-rss",
    "unexpected-clock", "base-clock", "cycle", "large-integer",
))
def test_reader_and_all_diagnostic_fields_match_inline_reference(tmp_path, monkeypatch, case):
    reference, namespace, _ = _inline_reference()
    path = tmp_path / "state"
    text = _state()
    now, barrier, include, active = 100.5, 0.0, False, [12]
    error = LookupError("original clock failure") if case == "unexpected-clock" else KeyboardInterrupt("original clock interrupt")
    if case == "inclusive": include = True
    if case == "header": text = text.replace(policy.TREE_STATE_SCHEMA, "wrong")
    if case == "timestamp": text = text.replace("100.0", "invalid")
    if case == "age": now = 102.01
    if case == "future": now = 99.0
    if case == "barrier": barrier = 100.01
    if case == "budget": text = text.replace("\n1000\n", "\n1001\n")
    if case == "fields": text += "14\t0\n"
    if case == "integer": text += "14\t0\tx\n"
    if case == "duplicate": text += "12\t11\t200\n"
    if case == "owner": text = text.replace("11\t10\t100\n", "")
    if case == "owner-rss": text = text.replace("11\t10\t100", "11\t10\t0")
    if case.startswith("active-"):
        include = True
        if case == "active-owner": active = [11]
        if case == "active-missing": active = [99]
        if case == "active-parent": text = text.replace("12\t11", "12\t10")
        if case == "active-rss": text = text.replace("12\t11\t200", "12\t11\t0")
    if case == "cycle": text += "14\t15\t9\n15\t14\t11\n"
    if case == "large-integer": text += "14\t0\t" + str(1 << 100) + "\n"
    if case != "missing": path.write_text(text)
    selected = "" if case == "empty" else str(path)
    def clock():
        if case in ("unexpected-clock", "base-clock"): raise error
        return now
    observed_time = SimpleNamespace(monotonic=clock)
    monkeypatch.setattr(policy, "time", observed_time)
    namespace["time"] = observed_time
    outcomes = []
    for reader in (reference, policy.read_tree_state):
        detail = {"stale": "must be cleared"}
        try:
            value = reader(selected, 1000, 11, active, barrier, include, detail)
            outcome = ("return", value)
        except BaseException as caught:
            assert caught is error
            outcome = ("raise", caught)
        outcomes.append((outcome, tuple(detail.items())))
    assert outcomes[0] == outcomes[1]
    assert len(outcomes[0][1]) == 15


@pytest.mark.parametrize("stage", ("clear", "update"))
@pytest.mark.parametrize("outcome", ("accepted", "rejected", "original-error"))
@pytest.mark.parametrize("kind", ("Exception", "BaseException"))
def test_diagnostic_failure_boundary_and_original_exception_identity(tmp_path, monkeypatch, stage, outcome, kind):
    reference, namespace, _ = _inline_reference()
    path = tmp_path / "state"
    path.write_text(_state())
    original = LookupError("original clock failure")
    diagnostic_error = RuntimeError("diagnostic failure") if kind == "Exception" else KeyboardInterrupt("diagnostic interrupt")
    def clock():
        if outcome == "original-error": raise original
        return 100.5 if outcome == "accepted" else 103.0
    observed_time = SimpleNamespace(monotonic=clock)
    monkeypatch.setattr(policy, "time", observed_time)
    namespace["time"] = observed_time
    results = []
    for reader in (reference, policy.read_tree_state):
        events = []
        class Diagnostic:
            def clear(self):
                events.append(("clear",))
                if stage == "clear": raise diagnostic_error
            def update(self, values):
                events.append(("update", tuple(values.items())))
                if stage == "update": raise diagnostic_error
        try:
            value = reader(str(path), 1000, 11, [12], diagnostic=Diagnostic())
            result = ("return", value)
        except BaseException as caught:
            assert caught is (diagnostic_error if kind == "BaseException" else original)
            result = ("raise", caught)
        results.append((result, events))
    assert results[0] == results[1]


def test_no_diagnostic_still_avoids_helper_call(monkeypatch):
    def forbidden(*_args):
        raise AssertionError("diagnostic=None must not call publisher")
    monkeypatch.setattr(policy, "_set_tree_state_diagnostic", forbidden)
    assert policy.read_tree_state("", 1000, 11, []) is None


def test_outlined_reader_strict_ir_and_precise_root_plans():
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    import re

    source = ("import time\nTREE_STATE_SCHEMA = " + repr(policy.TREE_STATE_SCHEMA)
              + "\nSTATE_MAX_AGE_SECONDS = " + repr(policy.STATE_MAX_AGE_SECONDS) + "\n"
              + inspect.getsource(policy._set_tree_state_diagnostic) + "\n"
              + inspect.getsource(policy.read_tree_state))
    module = infer_module(parse_and_lift(source, "reader.py", "reader"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = 'target triple = "x86_64-unknown-linux-gnu"\n' + codegen.generate(module)
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
    assert len(re.findall(r"\bcall\b[^\n]*@py_dict_new\(", text)) == 2
    functions = {function.name: function for function in codegen.module.functions}
    selected = [functions["user_reader_read_tree_state"], functions["user_reader__set_tree_state_diagnostic"]]
    assert sum(len(block._instrs) for function in selected for block in function.blocks) < 40000
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target="x86_64-linux")
    assert len(plans) == len(prepared.functions)
