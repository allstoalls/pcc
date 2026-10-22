"""Host source-body tests of native regex error classification.

These tests execute only the production ``_raise_engine_status`` function body
under CPython. Explicit boundary doubles record exception construction and
owned raising; they do not implement a regex matcher, native exception state,
GC, or the pcc compiler. The low-level status and CI checks are source-only.
Passing this file does not reproduce or repair a native matcher failure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "pcc/runtime/py/py_re_engine_runtime.py"
CORE = ROOT / "pcc/runtime/py/freestanding_re_engine.py"
WORKFLOW = ROOT / ".github/workflows/pcc1-package-parity.yml"


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _mapped_error(status: int) -> tuple[int, str]:
    function = _function(BRIDGE, "_raise_engine_status")
    constructed: list[tuple[int, str, object]] = []
    raised: list[object] = []

    def cstr(literal):
        assert isinstance(literal, str)
        return literal

    def py_exc_new(tag, message):
        exception = object()
        constructed.append((tag, message, exception))
        return exception

    def py_raise_owned(exception):
        raised.append(exception)

    namespace = {
        "cstr": cstr,
        "py_exc_new": py_exc_new,
        "py_raise_owned": py_raise_owned,
    }
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(BRIDGE), "exec"), namespace)
    assert namespace["_raise_engine_status"](status) is None
    assert len(constructed) == 1
    tag, message, exception = constructed[0]
    assert len(raised) == 1
    assert raised[0] is exception
    return tag, message


@pytest.mark.parametrize(
    ("status", "tag", "message"),
    [
        (
            -1,
            11,
            "pcc re: pattern outside the native regex subset (no-libpython)",
        ),
        (
            -4,
            11,
            "pcc re: non-ASCII text outside the native regex subset",
        ),
        (
            -2,
            7,
            "pcc re: native regex engine recursion-depth limit flag (status=-2)",
        ),
        (
            -3,
            7,
            "pcc re: native regex engine argument/capture-capacity failure (status=-3)",
        ),
    ],
    ids=["unsupported", "non-ascii", "recursion-limit", "bad-arguments"],
)
def test_known_status_preserves_tag_and_owned_raise(status, tag, message):
    assert _mapped_error(status) == (tag, message)


@pytest.mark.parametrize("status", [-99, -5, 0, 1])
def test_unknown_status_does_not_claim_a_limit(status):
    tag, message = _mapped_error(status)
    assert tag == 7
    assert message == "pcc re: native regex engine unexpected status"
    assert "limit" not in message


def test_error_mapping_uses_only_literal_messages_and_owned_raise():
    function = _function(BRIDGE, "_raise_engine_status")
    assert [argument.arg for argument in function.args.args] == ["status"]
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    assert {node.func.id for node in calls if isinstance(node.func, ast.Name)} == {
        "cstr",
        "py_exc_new",
        "py_raise_owned",
    }
    assert all(isinstance(node.func, ast.Name) for node in calls)
    for call in calls:
        if call.func.id == "cstr":
            assert len(call.args) == 1
            assert isinstance(call.args[0], ast.Constant)
            assert isinstance(call.args[0].value, str)
            assert not call.keywords


@pytest.mark.parametrize(
    ("condition", "status"),
    [
        (
            "ptr_is_null(pattern) != 0 or ptr_is_null(text) != 0 "
            "or ptr_is_null(caps) != 0 or ptr_is_null(groups_out) != 0",
            -3,
        ),
        ("caps_length < needed", -3),
        ("load_i32(context, 172) != 0", -2),
    ],
)
def test_low_level_source_status_contract(condition, status):
    function = _function(CORE, "_run_flags")
    expected = ast.dump(ast.parse(condition, mode="eval").body)
    branches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and ast.dump(node.test) == expected
    ]
    assert len(branches) == 1
    assert len(branches[0].body) == 1
    statement = branches[0].body[0]
    assert isinstance(statement, ast.Return)
    assert ast.literal_eval(statement.value) == status


def test_recursion_flag_source_is_set_at_the_existing_depth_guard():
    function = _function(CORE, "_match")
    guards = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "depth > 8192"
    ]
    assert len(guards) == 1
    assert ast.unparse(guards[0].body[0]) == "store_i32(context, 172, 1)"


def test_host_mapping_is_selected_in_existing_four_platform_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    test_path = "tests/test_native_regex_status_mapping.py"
    assert workflow.count(f'      - "{test_path}"') == 2
    commands = [
        line
        for line in workflow.splitlines()
        if "python -m pytest" in line and test_path in line
    ]
    assert len(commands) == 1
    assert "--junitxml=build/worker-prior-focused-host.xml" in commands[0]
    assert "test_recursion_limit_setter_range.py" in commands[0]
    for forbidden in ("--noconftest", "-n0", '-o "addopts="'):
        assert forbidden not in commands[0]
