"""Focused source for the finite native unittest / unittest.mock closure."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest as host_unittest
from unittest import mock as host_mock

import pytest

from pcc.stdlib import unittest as port_unittest
from pcc.stdlib.unittest import mock as port_mock


def _failure_message(case, method_name, *args):
    with pytest.raises(AssertionError) as raised:
        getattr(case, method_name)(*args, msg="detail")
    return str(raised.value)


def test_unittest_public_exports_match_cpython():
    assert port_unittest.__all__ == host_unittest.__all__
    assert port_mock.__all__ == host_mock.__all__


def test_testcase_core_assertions_match_cpython():
    host = host_unittest.TestCase()
    port = port_unittest.TestCase()

    for case in (host, port):
        case.assertEqual({"value": [1, 2]}, {"value": [1, 2]})
        case.assertNotEqual(1, 2)
        case.assertTrue("value")
        case.assertFalse("")
        case.assertIs(None, None)
        case.assertIsNot([], [])
        case.assertIsNone(None)
        case.assertIsNotNone(0)
        case.assertIn("son", "meson")
        case.assertNotIn("x", "meson")
        case.assertIsInstance("meson", str)
        case.assertNotIsInstance("meson", int)
        case.assertGreater(3, 2)
        case.assertGreaterEqual(3, 3)
        case.assertLess(2, 3)
        case.assertLessEqual(3, 3)
        case.assertAlmostEqual(1.0, 1.00000001)
        case.assertNotAlmostEqual(1.0, 1.1)
        case.assertListEqual([1, 2], [1, 2])
        case.assertTupleEqual((1, 2), (1, 2))
        case.assertDictEqual({"a": 1}, {"a": 1})
        case.assertSetEqual({1, 2}, {1, 2})
        case.assertRegex("meson-42", r"meson-\d+")
        case.assertNotRegex("meson", r"^ninja$")

    for method_name, args in (
        ("assertEqual", (1, 2)),
        ("assertTrue", (False,)),
        ("assertIn", ("x", "meson")),
    ):
        assert _failure_message(port, method_name, *args) == _failure_message(
            host, method_name, *args
        )


def _raise_value_error(value):
    raise ValueError("bad-" + str(value))


def test_assert_raises_callable_context_and_regex_match_cpython():
    for module in (host_unittest, port_unittest):
        case = module.TestCase()
        context = case.assertRaises(ValueError)
        with context:
            _raise_value_error(7)
        assert str(context.exception) == "bad-7"
        assert case.assertRaises(ValueError, _raise_value_error, 8) is None
        regex_context = case.assertRaisesRegex(ValueError, r"bad-9")
        with regex_context:
            _raise_value_error(9)
        assert str(regex_context.exception) == "bad-9"


def test_skip_decorators_and_unowned_runner_features_fail_closed():
    def value():
        return "kept"

    assert port_unittest.skipIf(False, "unused")(value) is value
    assert port_unittest.skipUnless(True, "unused")(value) is value
    skipped = port_unittest.skipIf(True, "finite reason")(value)
    with pytest.raises(port_unittest.SkipTest, match="finite reason"):
        skipped()
    with pytest.raises(port_unittest.SkipTest, match="direct reason"):
        port_unittest.TestCase().skipTest("direct reason")

    case = port_unittest.TestCase()
    with pytest.raises(NotImplementedError, match="subtest result"):
        case.subTest(name="unowned")
    with pytest.raises(NotImplementedError, match="reflective unittest discovery"):
        port_unittest.TestLoader().discover(".")
    with pytest.raises(NotImplementedError, match="result-driven execution"):
        port_unittest.TestSuite().run(port_unittest.TestResult())
    with pytest.raises(NotImplementedError, match="reflective unittest discovery"):
        port_unittest.main()
    with pytest.raises(NotImplementedError, match="event-loop lifecycle"):
        port_unittest.IsolatedAsyncioTestCase()
    expected = port_unittest.expectedFailure(value)
    with pytest.raises(NotImplementedError, match="expected-failure"):
        expected()


def test_suite_container_and_empty_result_match_cpython():
    host_cases = [host_unittest.TestCase(), host_unittest.TestCase()]
    port_cases = [port_unittest.TestCase(), port_unittest.TestCase()]
    host_suite = host_unittest.TestSuite(host_cases)
    port_suite = port_unittest.TestSuite(port_cases)
    assert len(list(port_suite)) == len(list(host_suite)) == 2
    assert port_suite.countTestCases() == host_suite.countTestCases() == 2
    assert port_unittest.TestResult().wasSuccessful()
    assert host_unittest.TestResult().wasSuccessful()


def _call_snapshot(mock_obj):
    current = None if mock_obj.call_args is None else tuple(mock_obj.call_args)
    return (
        mock_obj.called,
        mock_obj.call_count,
        current,
        [tuple(item) for item in mock_obj.call_args_list],
    )


def test_mock_explicit_calls_record_and_assert_like_cpython():
    host = host_mock.Mock(return_value="result", label="configured")
    port = port_mock.Mock(return_value="result", label="configured")
    assert host.label == port.label == "configured"
    assert host(3, flag=True) == port(3, flag=True) == "result"
    assert _call_snapshot(port) == _call_snapshot(host)

    port.assert_called()
    port.assert_called_once()
    port.assert_called_with(3, flag=True)
    port.assert_called_once_with(3, flag=True)
    port.assert_any_call(3, flag=True)
    port.assert_has_calls([port_mock.call(3, flag=True)])
    assert port.call_args == port_mock.call(3, flag=True)

    port.reset_mock()
    host.reset_mock()
    assert _call_snapshot(port) == _call_snapshot(host)
    port.assert_not_called()


def test_mock_callable_and_exception_side_effects_match_cpython():
    def multiply(value, factor=1):
        return value * factor

    host_callable = host_mock.Mock(side_effect=multiply)
    port_callable = port_mock.Mock(side_effect=multiply)
    assert port_callable(6, factor=7) == host_callable(6, factor=7) == 42
    assert _call_snapshot(port_callable) == _call_snapshot(host_callable)

    host_error = host_mock.Mock(side_effect=ValueError("side-effect"))
    port_error = port_mock.Mock(side_effect=ValueError("side-effect"))
    for mock_obj in (host_error, port_error):
        with pytest.raises(ValueError, match="side-effect"):
            mock_obj("recorded")
        assert mock_obj.call_count == 1


def test_mock_magic_and_automatic_children_fail_closed():
    assert callable(port_mock.Mock(return_value=None))
    assert callable(host_mock.Mock(return_value=None))
    assert not callable(port_mock.NonCallableMock())
    assert not callable(host_mock.NonCallableMock())
    with pytest.raises(NotImplementedError, match="automatic child return"):
        port_mock.Mock()()
    with pytest.raises(NotImplementedError, match="automatic child return"):
        port_mock.Mock().return_value
    with pytest.raises(NotImplementedError, match="automatic child attribute"):
        port_mock.Mock().child
    with pytest.raises(NotImplementedError, match="iterable"):
        port_mock.Mock(side_effect=[1, 2])()
    with pytest.raises(NotImplementedError, match="exception-class"):
        port_mock.Mock(side_effect=ValueError)()
    with pytest.raises(NotImplementedError, match="magic-method synthesis"):
        port_mock.MagicMock()
    with pytest.raises(NotImplementedError, match="await accounting"):
        port_mock.AsyncMock()
    with pytest.raises(NotImplementedError, match="sentinel creation"):
        port_mock.sentinel.dynamic
    with pytest.raises(NotImplementedError, match="autospeccing"):
        port_mock.create_autospec(object)


class _PatchTarget:
    def __init__(self):
        self.value = "original"

    def work(self, value):
        return "work-" + value


def test_patch_object_is_transactional_and_supports_explicit_default_mock():
    target = _PatchTarget()
    with port_mock.patch.object(target, "value", "patched") as replacement:
        assert replacement == "patched"
        assert target.value == "patched"
    assert target.value == "original"
    with pytest.raises(RuntimeError, match="body"):
        with port_mock.patch.object(target, "value", "temporary"):
            raise RuntimeError("body")
    assert target.value == "original"

    with port_mock.patch.object(target, "created", 7, create=True):
        assert target.created == 7
    assert not hasattr(target, "created")

    with port_mock.patch.object(target, "work") as replacement:
        replacement.return_value = "mocked"
        assert target.work("input") == "mocked"
        replacement.assert_called_once_with("input")
    assert target.work("input") == "work-input"
    assert "work" not in target.__dict__

    patcher = port_mock.patch.object(target, "value", "started")
    assert patcher.start() == "started"
    assert target.value == "started"
    assert patcher.stop() is None
    assert target.value == "original"


def test_patch_dict_matches_cpython_for_concrete_dictionaries():
    host_values = {"kept": "host", "old": "value"}
    port_values = {"kept": "host", "old": "value"}
    with host_mock.patch.dict(host_values, {"added": "new"}):
        host_inside = dict(host_values)
    with port_mock.patch.dict(port_values, {"added": "new"}):
        port_inside = dict(port_values)
    assert port_inside == host_inside
    assert port_values == host_values == {"kept": "host", "old": "value"}
    with pytest.raises(RuntimeError, match="body"):
        with port_mock.patch.dict(port_values, {"temporary": "value"}):
            raise RuntimeError("body")
    assert port_values == host_values

    with host_mock.patch.dict(host_values, {"only": "item"}, clear=True):
        host_clear = dict(host_values)
    with port_mock.patch.dict(port_values, {"only": "item"}, clear=True):
        port_clear = dict(port_values)
    assert port_clear == host_clear == {"only": "item"}
    assert port_values == host_values

    class Mapping:
        def __init__(self):
            self.data = {"value": 1}

        def __iter__(self):
            return iter(self.data)

        def __getitem__(self, key):
            return self.data[key]

        def __setitem__(self, key, value):
            self.data[key] = value

        def __delitem__(self, key):
            del self.data[key]

    with pytest.raises(NotImplementedError, match="clear/update"):
        with port_mock.patch.dict(Mapping(), {"new": 2}):
            pass

    env_key = "PCC_UNITTEST_MOCK_FOCUSED_ENV"
    old_present = env_key in os.environ
    old_value = os.environ.get(env_key)
    with port_mock.patch.dict(os.environ, {env_key: "patched"}):
        assert os.environ[env_key] == "patched"
    assert (env_key in os.environ) is old_present
    if old_present:
        assert os.environ[env_key] == old_value


def test_patch_string_and_single_decorator_are_generic_and_fail_closed_when_stacked():
    module_name = "pcc.stdlib.unittest.mock.FILTER_DIR"
    assert port_mock.FILTER_DIR is True
    with port_mock.patch(module_name, False):
        assert port_mock.FILTER_DIR is False
    assert port_mock.FILTER_DIR is True

    target = _PatchTarget()

    @port_mock.patch.object(target, "value")
    def decorated(replacement):
        replacement.return_value = "unused"
        return target.value is replacement

    assert decorated() is True
    assert target.value == "original"

    @port_mock.patch.object(target, "value", "explicit")
    def explicit_replacement():
        return target.value

    assert explicit_replacement() == "explicit"
    assert target.value == "original"
    with pytest.raises(NotImplementedError, match="stacked"):
        port_mock.patch.object(target, "value", "again")(decorated)


@pytest.mark.parametrize("module_name", ["unittest", "unittest.mock"])
def test_unittest_family_is_selected_by_recursive_stdlib_registry(module_name):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source(module_name)
    assert source is not None
    if module_name == "unittest":
        assert source.endswith("/pcc/stdlib/unittest/__init__.py")
    else:
        assert source.endswith("/pcc/stdlib/unittest/mock.py")
    assert pipeline._classify_python_import(module_name) == "native_stdlib"
    assert module_name not in pipeline._NATIVE_BUILTIN_IMPORTS


def test_recursive_unittest_provider_admits_only_explicit_mock_sibling(tmp_path):
    from pcc.frontends.python import pipeline

    package_only = tmp_path / "package_only.py"
    package_only.write_text("import unittest\n", encoding="utf-8")
    only_sources, only_seed_modules = pipeline._collect_relative_module_closure(
        str(package_only)
    )
    _only_sources, only_modules = pipeline._collect_multi_source_relative_closure(
        only_sources,
        only_seed_modules,
        recursive_stdlib=True,
    )
    assert "unittest" in only_modules
    assert "unittest.mock" not in only_modules

    entry = tmp_path / "entry.py"
    entry.write_text(
        "import unittest\nfrom unittest import mock\n",
        encoding="utf-8",
    )
    seed_sources, seed_modules = pipeline._collect_relative_module_closure(
        str(entry)
    )
    _sources, modules = pipeline._collect_multi_source_relative_closure(
        seed_sources,
        seed_modules,
        recursive_stdlib=True,
    )
    assert "unittest" in modules
    assert "unittest.mock" in modules


@pytest.mark.integration
def test_unittest_and_mock_match_cpython_strict_self_no_libpython(tmp_path):
    source = '''\
import unittest
from unittest import mock

class Target:
    def __init__(self):
        self.value = "original"

    def work(self, value):
        return "work-" + value

case = unittest.TestCase()
case.assertEqual({"value": [1, 2]}, {"value": [1, 2]})
case.assertTrue("meson")
case.assertIn("son", "meson")
case.assertRegex("meson-42", r"meson-\\d+")
with case.assertRaises(ValueError) as raised:
    raise ValueError("owned-error")
print("assertions", str(raised.exception))

@unittest.skipIf(False, "not skipped")
def kept():
    return "kept"
print("skip-false", kept())

explicit = mock.Mock(return_value="result", label="configured")
print("mock-call", explicit(3, flag=True), explicit.label)
print(
    "mock-record",
    explicit.called,
    explicit.call_count,
    explicit.call_args[0][0],
    explicit.call_args[1]["flag"],
)
explicit.assert_called_once_with(3, flag=True)

target = Target()
with mock.patch.object(target, "value", "patched"):
    print("patch-object-inside", target.value)
print("patch-object-after", target.value)

with mock.patch.object(target, "work") as replacement:
    replacement.return_value = "mocked"
    print("patch-default", target.work("input"), replacement.call_count)
print("patch-method-after", target.work("input"))

values = {"kept": "old", "removed": "value"}
with mock.patch.dict(values, {"only": "new"}, clear=True):
    print("patch-dict-inside", sorted(values.items()))
print("patch-dict-after", sorted(values.items()))

try:
    mock.Mock()()
except NotImplementedError:
    print("mock-default fail-closed")
else:
    print("mock-default automatic")
'''
    src = tmp_path / "unittest_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "unittest_probe"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)

    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=900,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert "PCC-PY-COMPILE-001" not in build.stdout + build.stderr

    no_host_env = env.copy()
    no_host_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    no_host_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=120,
        env=no_host_env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr

    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert "mock-default fail-closed\n" in actual.stdout
    assert "mock-default automatic\n" in expected.stdout
    assert actual.stdout.replace(
        "mock-default fail-closed\n", "mock-default automatic\n"
    ) == expected.stdout
