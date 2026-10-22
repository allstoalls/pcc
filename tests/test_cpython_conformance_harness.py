"""The CPython ``Lib/test`` conformance harness (scripts/cpython_conformance).

A synthetic checkout with one test module covers the runner's case
accounting under CPython -- every status CPython's TestCase.run can give a
case -- and the harness's plan, scoring and failure categories; the native
case compiles that module with pcc and replays the baseline plan.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "cpython_conformance" / "run.py"


def _harness():
    spec = importlib.util.spec_from_file_location("cpython_conformance_run", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SYNTHETIC = '''
import unittest
from test import support


class Basic(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(sum([1, 2, 3]), 6)

    def test_fail(self):
        self.assertEqual(1, 2)

    def test_error(self):
        raise KeyError("missing")

    @unittest.skip("not today")
    def test_skip_decorator(self):
        raise AssertionError("never runs")

    def test_skip_call(self):
        self.skipTest("skipped inside")

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(1, 2)

    @unittest.expectedFailure
    def test_unexpected_success(self):
        pass

    def test_failing_cleanup(self):
        def cleanup():
            raise ValueError("cleanup")
        self.addCleanup(cleanup)

    @support.cpython_only
    def test_cpython_only(self):
        pass

    def helper_not_a_test(self):
        raise AssertionError("never collected")


class SetUpSkips(unittest.TestCase):
    def setUp(self):
        raise unittest.SkipTest("setUp skips")

    def test_one(self):
        pass


class ClassSkips(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest("class skipped")

    def test_one(self):
        pass


class ClassErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("class broken")

    def test_one(self):
        pass
'''


EXPECTED_BASELINE = {
    "test.test_synthetic.Basic.test_pass": "PASS",
    "test.test_synthetic.Basic.test_fail": "FAIL",
    "test.test_synthetic.Basic.test_error": "ERROR",
    "test.test_synthetic.Basic.test_skip_decorator": "SKIP",
    "test.test_synthetic.Basic.test_skip_call": "SKIP",
    "test.test_synthetic.Basic.test_expected_failure": "XFAIL",
    "test.test_synthetic.Basic.test_unexpected_success": "UPASS",
    "test.test_synthetic.Basic.test_failing_cleanup": "ERROR",
    "test.test_synthetic.Basic.test_cpython_only": "SKIP",
    "test.test_synthetic.SetUpSkips.test_one": "SKIP",
    "test.test_synthetic.ClassSkips.test_one": "SKIP",
    "test.test_synthetic.ClassErrors.test_one": "ERROR",
}


# Plain test methods and class/case set-up only: the native case measures the
# harness (plan replay, ids, statuses), not pcc's decorator or cleanup support.
SYNTHETIC_NATIVE = '''
import unittest


class Plain(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(sum([1, 2, 3]), 6)

    def test_fail(self):
        self.assertEqual(1, 2)

    def test_error(self):
        raise KeyError("missing")

    def test_skip_call(self):
        self.skipTest("skipped inside")


class SetUpSkips(unittest.TestCase):
    def setUp(self):
        raise unittest.SkipTest("setUp skips")

    def test_one(self):
        pass


class ClassErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("class broken")

    def test_one(self):
        pass
'''


def _checkout(root: Path) -> Path:
    lib_test = root / "Lib" / "test"
    (lib_test / "support").mkdir(parents=True)
    (lib_test / "__init__.py").write_text("", encoding="utf-8")
    for name in ("testcase.py", "numbers.py"):
        (lib_test / "support" / name).write_text("", encoding="utf-8")
    (lib_test / "test_synthetic.py").write_text(textwrap.dedent(SYNTHETIC), encoding="utf-8")
    (lib_test / "test_synthetic_native.py").write_text(
        textwrap.dedent(SYNTHETIC_NATIVE), encoding="utf-8")
    return root


def _paths(harness, out: Path) -> dict:
    paths = {"out": out, "stage": out / "stage", "work": out / "work",
             "results": out / "results", "bin": out / "bin"}
    for name in ("results", "work", "bin"):
        paths[name].mkdir(parents=True, exist_ok=True)
    return paths


def test_runner_accounts_every_case_status_under_cpython(tmp_path):
    harness = _harness()
    checkout = _checkout(tmp_path / "cpython")
    paths = _paths(harness, tmp_path / "out")
    harness.stage(checkout, paths["stage"])
    args = SimpleNamespace(python=sys.executable, run_timeout=60, baseline_only=True)
    result = harness.run_module("test_synthetic", args, paths, "key")
    statuses = {case_id: status for case_id, (status, _detail) in result["baseline"].items()}
    assert statuses == EXPECTED_BASELINE
    assert result["cpython"]["done"] == len(EXPECTED_BASELINE)
    # The plan lists each class's tests in loader order under its attribute.
    plan = {attribute: names for attribute, _class_id, names in result["plan"]}
    assert list(plan) == ["Basic", "ClassErrors", "ClassSkips", "SetUpSkips"]
    assert plan["Basic"] == sorted(name for name in plan["Basic"])
    assert "helper_not_a_test" not in plan["Basic"]
    assert result["baseline"]["test.test_synthetic.Basic.test_failing_cleanup"][1] == \
        "a cleanup function raised"
    assert result["score"]["baseline_success"] == 2  # test_pass and test_expected_failure


def test_score_counts_only_baseline_successes():
    harness = _harness()
    baseline = {"a": ["PASS", ""], "b": ["XFAIL", ""], "c": ["FAIL", ""], "d": ["PASS", ""]}
    native = {"a": ["PASS", ""], "b": ["ERROR", "x"], "c": ["PASS", ""], "e": ["PASS", ""]}
    assert harness.score(baseline, native) == {
        "baseline_cases": 4,
        "baseline_success": 3,
        "native_success": 1,
        "native_unreported": 1,
        "native_only": ["e"],
    }


def test_output_parsing_plan_cases_and_done(tmp_path):
    harness = _harness()
    output = tmp_path / "out.txt"
    output.write_text(
        "noise from a test\n"
        "@@PCC-CONFORMANCE@@ CLASS Basic test.m.Basic test_a test_b\n"
        "@@PCC-CONFORMANCE@@ CASE PASS test.m.Basic.test_a\n"
        "@@PCC-CONFORMANCE@@ CASE FAIL test.m.Basic.test_b :: AssertionError: 1 != 2\n"
        "@@PCC-CONFORMANCE@@ DONE 2\n",
        encoding="utf-8",
    )
    parsed = harness.parse_output(output)
    assert parsed == {
        "plan": [["Basic", "test.m.Basic", ["test_a", "test_b"]]],
        "cases": {"test.m.Basic.test_a": ["PASS", ""],
                  "test.m.Basic.test_b": ["FAIL", "AssertionError: 1 != 2"]},
        "done": 2,
    }
    driver = harness.native_driver("test_m", parsed["plan"])
    assert "from test import test_m as conformance_module" in driver
    assert "('Basic', 'test.m.Basic', ['test_a', 'test_b'])," in driver
    assert driver.rstrip().endswith("run_plan(conformance_module, PLAN)")


def test_failure_categories_mask_varying_text():
    harness = _harness()
    assert harness.category("ERROR AttributeError: 'int' object has no attribute 'real'") == \
        "ERROR AttributeError: '…' object has no attribute '…'"
    assert harness.category("FAIL AssertionError: 12 != 13") == "FAIL AssertionError: N != N"
    assert harness.category(
        "ERROR NotImplementedError: no-libpython function unavailable: test.test_x.test_y"
    ) == "ERROR NotImplementedError: no-libpython function unavailable: …"


def test_output_directory_must_be_outside_the_repository(tmp_path):
    harness = _harness()
    with pytest.raises(SystemExit):
        harness.main(["--cpython-src", str(tmp_path), "--out", str(harness.REPO / "build" / "x")])


@pytest.mark.integration
def test_native_replay_matches_baseline_ids(tmp_path, pcc_runtime_archive):
    harness = _harness()
    checkout = _checkout(tmp_path / "cpython")
    paths = _paths(harness, tmp_path / "out")
    harness.stage(checkout, paths["stage"])
    args = SimpleNamespace(
        python=sys.executable, run_timeout=120, compile_timeout=900, baseline_only=False,
        runtime_archive=str(pcc_runtime_archive),
        pcc=f"uv run --project {harness.REPO} pcc",
    )
    result = harness.run_module("test_synthetic_native", args, paths, "key")
    assert not result["native"]["compile"]["error"], result["native"]["compile"]
    run = result["native"]["run"]
    baseline = {case_id: status for case_id, (status, _detail) in result["baseline"].items()}
    native = {case_id: status for case_id, (status, _detail) in result["native_cases"].items()}
    assert baseline == {
        "test.test_synthetic_native.Plain.test_pass": "PASS",
        "test.test_synthetic_native.Plain.test_fail": "FAIL",
        "test.test_synthetic_native.Plain.test_error": "ERROR",
        "test.test_synthetic_native.Plain.test_skip_call": "SKIP",
        "test.test_synthetic_native.SetUpSkips.test_one": "SKIP",
        "test.test_synthetic_native.ClassErrors.test_one": "ERROR",
    }
    assert run["done"] == len(baseline), run
    assert native == baseline, result["native_cases"]
    assert result["score"]["baseline_success"] == result["score"]["native_success"] == 1


def test_remove_tree_handles_entries_a_test_left_without_permissions(tmp_path):
    # test_dbm_sqlite3 leaves a read-only directory with content in its cwd.
    harness = _harness()
    locked = tmp_path / "work" / "module" / "@test_tmp"
    (locked / "inner" / "deeper").mkdir(parents=True)
    (locked / "inner" / "file").write_text("x", encoding="utf-8")
    # test_tarfile leaves a directory without search permission.
    os.chmod(locked / "inner" / "deeper", 0o000)
    os.chmod(locked / "inner", 0o500)
    os.chmod(locked, 0o500)
    harness.remove_tree(tmp_path / "work")
    assert not (tmp_path / "work").exists()


def test_packages_and_their_modules_are_measured_under_dotted_names(tmp_path):
    harness = _harness()
    checkout = _checkout(tmp_path / "cpython")
    lib_test = checkout / "Lib" / "test"
    package = lib_test / "test_pkg"
    (package / "inner").mkdir(parents=True)
    (package / "data").mkdir()
    (package / "__init__.py").write_text(
        "import unittest\n\nclass InInit(unittest.TestCase):\n    def test_here(self):\n        pass\n",
        encoding="utf-8")
    (package / "test_sub.py").write_text(
        "import unittest\nfrom test.test_pkg import InInit\n\n"
        "class Sub(unittest.TestCase):\n    def test_sub(self):\n        self.assertTrue(InInit)\n",
        encoding="utf-8")
    (package / "inner" / "__init__.py").write_text("", encoding="utf-8")
    (package / "inner" / "test_deep.py").write_text(
        "import unittest\n\nclass Deep(unittest.TestCase):\n    def test_deep(self):\n        pass\n",
        encoding="utf-8")
    # A data directory is not a package: nothing below it is a test module.
    (package / "data" / "test_data_file.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    assert harness.discover_modules(checkout) == [
        "test_pkg", "test_pkg.inner.test_deep", "test_pkg.test_sub",
        "test_synthetic", "test_synthetic_native",
    ]
    assert harness.import_line("test_pkg.inner.test_deep") == \
        "from test.test_pkg.inner import test_deep as conformance_module"
    paths = _paths(harness, tmp_path / "out")
    harness.stage(checkout, paths["stage"])
    args = SimpleNamespace(python=sys.executable, run_timeout=60, baseline_only=True)
    expected = {
        "test_pkg": {"test.test_pkg.InInit.test_here": "PASS"},
        "test_pkg.test_sub": {"test.test_pkg.InInit.test_here": "PASS",
                              "test.test_pkg.test_sub.Sub.test_sub": "PASS"},
        "test_pkg.inner.test_deep": {"test.test_pkg.inner.test_deep.Deep.test_deep": "PASS"},
    }
    for module, cases in expected.items():
        result = harness.run_module(module, args, paths, "key")
        assert {case_id: status for case_id, (status, _) in result["baseline"].items()} == cases
        assert (paths["results"] / f"{module}.json").exists()
