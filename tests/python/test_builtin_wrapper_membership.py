"""Live wrapper membership without rebuilding the complete name snapshot."""

from types import SimpleNamespace

import pytest

from pcc.frontends.python import builtin_function_values as values, py_ast
from pcc.frontends.python.codegen import name_lowering as names


class _Probe(names.NameLoweringMixin):
    def __init__(self, name, wrapper=True):
        self.functions = {values.BUILTIN_VALUE_PREFIX + name: wrapper}
        self.env = {}
        self._module_globals = {}
        self.current_func_def = None


def _legacy(probe, call, name):
    if probe._builtin_function_wrapper(name) is None:
        return False
    if name in probe.env or name in probe._module_globals or name in probe.functions:
        return False
    current = probe.current_func_def
    if current is not None and current.name.startswith(values.BUILTIN_HELPER_PREFIX):
        return False
    return values.builtin_call_needs_wrapper(name, call)


def _outcome(action):
    try:
        return ("return", action())
    except Exception as error:
        return (type(error).__name__, str(error))


@pytest.mark.parametrize("name", tuple(values._SOURCES) + ("ordinary_function",))
@pytest.mark.parametrize("kwargs", ((), (("unexpected", None),)))
def test_wrapper_decision_matches_legacy_for_all_builtin_names(name, kwargs):
    probe = _Probe(name)
    call = SimpleNamespace(args=(), kwargs=kwargs)
    assert probe._builtin_call_takes_wrapper(call, name) == _legacy(probe, call, name)


def test_membership_observes_live_registry_changes_without_mutating_snapshots(monkeypatch):
    registry = dict(values._SOURCES)
    monkeypatch.setattr(values, "_SOURCES", registry)
    before = values.builtin_function_value_names()
    name = "new_test_builtin"
    assert not values.builtin_function_value_name_exists(name)
    registry[name] = ((), "unused source")
    assert values.builtin_function_value_name_exists(name)
    assert name not in before
    after = values.builtin_function_value_names()
    assert name in after and isinstance(after, frozenset)
    del registry[name]
    assert not values.builtin_function_value_name_exists(name)
    assert name in after
    monkeypatch.setattr(values, "_SOURCES", {name: ((), "replacement")})
    assert values.builtin_function_value_name_exists(name)
    assert not values.builtin_function_value_name_exists("len")


@pytest.mark.parametrize("name", (None, 1, [], set(), {"len"}, frozenset({"len"})))
def test_nonstring_membership_retains_legacy_result_or_error(name):
    expected = _outcome(lambda: name in values.builtin_function_value_names())
    assert _outcome(lambda: values.builtin_function_value_name_exists(name)) == expected


def test_string_subclass_and_custom_registry_keep_materializing_fallback(monkeypatch):
    class CustomName(str):
        pass

    class CustomRegistry(dict):
        pass

    original = values.builtin_function_value_names
    calls = []

    def snapshot():
        calls.append("snapshot")
        return original()

    monkeypatch.setattr(values, "builtin_function_value_names", snapshot)
    assert values.builtin_function_value_name_exists(CustomName("len"))
    monkeypatch.setattr(values, "_SOURCES", CustomRegistry(values._SOURCES))
    assert values.builtin_function_value_name_exists("len")
    assert calls == ["snapshot", "snapshot"]


def test_absent_name_does_not_read_functions_or_call_shape():
    class AbsentProbe(names.NameLoweringMixin):
        @property
        def functions(self):
            pytest.fail("absent name must stop before functions")

    assert not AbsentProbe()._builtin_call_takes_wrapper(object(), "ordinary_function")


@pytest.mark.parametrize("wrapper", (None, False, 0, ()))
def test_wrapper_lookup_distinguishes_none_from_falsey_values(wrapper):
    probe = _Probe("len", wrapper)
    call = SimpleNamespace(args=(), kwargs=(("unexpected", None),))
    assert probe._builtin_call_takes_wrapper(call, "len") is (wrapper is not None)
    assert probe._builtin_call_takes_wrapper(call, "len") == _legacy(probe, call, "len")


@pytest.mark.parametrize("reason", ("missing", "none", "local", "global", "function", "helper"))
def test_early_exits_do_not_inspect_call_arguments(reason):
    probe = _Probe("len")
    if reason == "missing":
        probe.functions.clear()
    elif reason == "none":
        probe.functions[values.BUILTIN_VALUE_PREFIX + "len"] = None
    elif reason == "local":
        probe.env["len"] = None
    elif reason == "global":
        probe._module_globals["len"] = None
    elif reason == "function":
        probe.functions["len"] = None
    else:
        probe.current_func_def = SimpleNamespace(name=values.BUILTIN_HELPER_PREFIX + "probe")
    assert probe._builtin_call_takes_wrapper(object(), "len") is False
    assert _legacy(probe, object(), "len") is False


def test_wrapper_lookup_error_still_precedes_shadowing():
    class BrokenFunctions(dict):
        def get(self, key):
            raise RuntimeError("wrapper lookup")

    probe = _Probe("len")
    probe.functions = BrokenFunctions(probe.functions)
    probe.env["len"] = None
    assert _outcome(lambda: probe._builtin_call_takes_wrapper(object(), "len")) == (
        "RuntimeError", "wrapper lookup",
    )
    assert _outcome(lambda: _legacy(probe, object(), "len")) == ("RuntimeError", "wrapper lookup")


def test_present_wrapper_retains_malformed_call_error():
    probe = _Probe("len")
    assert _outcome(lambda: probe._builtin_call_takes_wrapper(object(), "len")) == _outcome(
        lambda: _legacy(probe, object(), "len"),
    )


def test_unpack_and_print_keyword_routes_remain_unchanged():
    starred = py_ast.Call(None, None, py_ast.Name(None, None, "*"), (), ())
    shapes = (
        ("len", (starred,), ()),
        ("print", (starred,), ()),
        ("print", (), (("flush", py_ast.BoolLit(None, None, True)),)),
        ("print", (), (("flush", py_ast.Name(None, None, "flag")),)),
        ("print", (), (("flush", py_ast.Name(None, None, "flag")), ("file", None))),
        ("sorted", (), (("key", None),)),
    )
    for name, args, kwargs in shapes:
        probe = _Probe(name)
        call = SimpleNamespace(args=args, kwargs=kwargs)
        assert probe._builtin_call_takes_wrapper(call, name) == _legacy(probe, call, name)


def test_exact_string_hot_calls_avoid_snapshot_construction(monkeypatch):
    original = values.builtin_function_value_names
    snapshots = []

    def snapshot():
        snapshots.append(1)
        return original()

    monkeypatch.setattr(names, "builtin_function_value_names", snapshot)
    monkeypatch.setattr(values, "builtin_function_value_names", snapshot)
    call = SimpleNamespace(args=(), kwargs=())
    spellings = tuple(values._SOURCES) + ("ordinary_function",)
    for spelling in spellings:
        assert _Probe(spelling)._builtin_call_takes_wrapper(call, spelling) is False
    assert snapshots == []
    for spelling in spellings:
        assert _legacy(_Probe(spelling), call, spelling) is False
    assert len(snapshots) == len(spellings)
