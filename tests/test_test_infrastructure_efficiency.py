"""Structural guards against repeating native-runtime builds in pytest.

The runtime probes intentionally compile real pcc-Python archives.  The
guard is about build ownership: a configuration is built once per pytest
process/fixture, probe sources remain per-test, and no test deletes shared
repository build products to force a rebuild.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).absolute().parents[1]
TESTS = ROOT / "tests"
THIS_FILE = Path(__file__).absolute()

# Runtime variants are built only by tests/runtime_build_cache.py; a test that
# copies the runtime tree and force-rebuilds it must be listed here with the
# marker that bounds how often it runs.
_RUNTIME_COPY_BUILD_STRATEGIES: dict[str, str] = {}

_STATELESS_GC_COMPILE_SUITES = (
    "tests/python/test_gc_api.py",
    "tests/python/test_gc_effectiveness.py",
    "tests/python/test_gc_finalizer_corner.py",
    "tests/python/test_gc_performance.py",
    "tests/python/test_gc_regression_bugs.py",
    "tests/python/test_gc_resurrection.py",
    "tests/python/test_gc_trashcan.py",
)

_HEAVY_EXTERNAL_CORPUS_SUITES = (
    "tests/c/test_c_testsuite.py",
    "tests/c/test_c_testsuite_self.py",
    "tests/c/test_gcc_torture_execute.py",
    "tests/c/test_gcc_torture_self.py",
)

_FULL_BOOTSTRAP_INTEGRATION_GATES = tuple(
    f"tests/python/gc/test_pcc_bootstrap_full_gc{backend}.py" for backend in range(5)
)

_RUNTIME_SOURCE_COPY_SUITES = (
    *_RUNTIME_COPY_BUILD_STRATEGIES,
    "tests/runtime_build_cache.py",
)

_NESTED_PYTEST_STRATEGIES = {
    "tests/python/test_gc_backend_under_env.py": "One inner pytest owns the complete frontend/GC slice",
    "tests/test_gc_bootstrap_xdist_group.py": "test_full_gc_bootstraps_remain_independently_schedulable",
    # Inner runs over a two-test temporary file under ``-c /dev/null``.
    "tests/python/test_pytest_live_report_tool.py": '"/dev/null"',
    # Inner runs only collect a temporary source tree, bounded by timeout=15.
    "tests/python/test_install_pcc1_toolchain.py": '"--collect-only"',
    # Inner runs execute only generated, compiler-free probes with timeout=30.
    "tests/test_xdist_cpu_budget.py": "test_real_xdist_budget_contract",
}
_NESTED_PYTEST_ARGV = re.compile(r"""["']-m["'],\s*["']pytest["']""")


def _pcc_gate_test_item(*markers, nodeid="probe"):
    return SimpleNamespace(
        nodeid=nodeid,
        iter_markers=lambda name: iter(mark for mark in markers if mark.name == name),
    )


@pytest.mark.parametrize("available", [True, False])
def test_pcc_gate_callable_probe_runs_once(available):
    from tests.conftest import _pcc_gate_blocked_reason

    calls = []

    def probe():
        calls.append(True)
        return available

    item = _pcc_gate_test_item(pytest.mark.pcc_gate(probe=probe).mark)
    reason = _pcc_gate_blocked_reason(item)
    assert calls == [True]
    assert reason == (None if available else "callable probe returned False")


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TypeError])
def test_pcc_gate_callable_probe_errors_are_not_deselections(error_type):
    from tests.conftest import _pcc_gate_blocked_reason

    error = error_type("broken probe")
    calls = []

    def probe():
        calls.append(True)
        raise error

    item = _pcc_gate_test_item(pytest.mark.pcc_gate(probe=probe).mark)
    with pytest.raises(error_type, match="broken probe") as raised:
        _pcc_gate_blocked_reason(item)
    assert raised.value is error
    assert calls == [True]


@pytest.mark.parametrize("result", [None, 0, 1, "unavailable", [], object()])
def test_pcc_gate_callable_probe_requires_boolean_result(result):
    from tests.conftest import _pcc_gate_blocked_reason

    calls = []

    def probe():
        calls.append(True)
        return result

    item = _pcc_gate_test_item(pytest.mark.pcc_gate(probe=probe).mark)
    with pytest.raises(TypeError, match="pcc_gate callable probe must return bool"):
        _pcc_gate_blocked_reason(item)
    assert calls == [True]


@pytest.mark.parametrize(
    "sys_platform,system,machine,available",
    [
        ("linux", "Linux", "x86_64", True),
        ("darwin", "Darwin", "arm64", False),
        ("darwin", "Darwin", "x86_64", False),
        ("linux", "Linux", "aarch64", False),
    ],
)
def test_pcc_gate_linux_execution_markers_respect_simulated_platform(
    monkeypatch, sys_platform, system, machine, available
):
    from tests.conftest import pytest_collection_modifyitems
    from tests.python import (
        test_direct_indexed_syscall,
        test_freestanding_libc_numeric,
        test_freestanding_linux_libc,
        test_typed_indirect_c_abi,
    )

    def unexpected_execution(*args, **kwargs):
        raise AssertionError("platform gate simulation must not execute binaries")

    monkeypatch.setattr(subprocess, "run", unexpected_execution)
    platform = SimpleNamespace(system=lambda: system, machine=lambda: machine)
    items = []
    for module in (
        test_direct_indexed_syscall,
        test_freestanding_libc_numeric,
        test_freestanding_linux_libc,
        test_typed_indirect_c_abi,
    ):
        monkeypatch.setattr(module, "platform", platform)
        if hasattr(module, "sys"):
            monkeypatch.setattr(module, "sys", SimpleNamespace(platform=sys_platform))
        for name, function in vars(module).items():
            if not name.startswith("test_"):
                continue
            for marker in getattr(function, "pytestmark", ()):
                if marker.name == "pcc_gate" and callable(marker.kwargs.get("probe")):
                    items.append(_pcc_gate_test_item(marker, nodeid=name))
    # Seven Linux x86_64 execution gates, and one (e190cc4f3) that also runs
    # on Darwin arm64.
    also_darwin_arm64 = {"test_numeric_errno_and_fenv_contract_executes_in_both_backends"}
    assert len(items) == 8
    assert {item.nodeid for item in items} >= also_darwin_arm64
    original = items.copy()
    expected = [
        item for item in original
        if available or (
            item.nodeid in also_darwin_arm64
            and (sys_platform, system, machine) == ("darwin", "Darwin", "arm64")
        )
    ]
    deselected = []
    config = SimpleNamespace(
        hook=SimpleNamespace(pytest_deselected=lambda *, items: deselected.extend(items))
    )
    pytest_collection_modifyitems(config, items)
    assert items == expected
    assert deselected == [item for item in original if item not in expected]


def _python_test_sources():
    for path in TESTS.rglob("*.py"):
        if path.absolute() != THIS_FILE:
            yield path, path.read_text(encoding="utf-8")


def test_all_copy_and_force_build_tests_have_an_audited_reuse_strategy():
    discovered = {
        path.relative_to(ROOT).as_posix()
        for path, source in _python_test_sources()
        if "copytree(" in source and '"-B"' in source
    }
    assert discovered == set(_RUNTIME_COPY_BUILD_STRATEGIES)
    for relative, marker in _RUNTIME_COPY_BUILD_STRATEGIES.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert marker in source, f"{relative} lost audited build strategy {marker!r}"


def test_nested_pytest_invocations_are_finite_and_audited():
    # File-level matching intentionally covers both an inline argv and the
    # common ``cmd = [...]`` followed by ``subprocess.run(cmd)`` form.
    discovered = {
        path.relative_to(ROOT).as_posix()
        for path, source in _python_test_sources()
        if "subprocess.run(" in source and _NESTED_PYTEST_ARGV.search(source)
    }

    assert discovered == set(_NESTED_PYTEST_STRATEGIES)
    for relative, marker in _NESTED_PYTEST_STRATEGIES.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert marker in source, f"{relative} lost audited strategy {marker!r}"


def test_l1_codegen_static_method_table_matches_host_contract():
    import inspect

    from pcc.frontends.python.codegen._l1_codegen_static_methods import (
        L1_CODEGEN_STATIC_METHODS,
    )
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    generated_names = tuple(entry["name"] for entry in L1_CODEGEN_STATIC_METHODS)
    assert generated_names == L1_CODEGEN_HOST_METHODS
    assert len(generated_names) == len(set(generated_names))

    expected = []
    for method_name in L1_CODEGEN_HOST_METHODS:
        signature = inspect.signature(getattr(L1CodeGen, method_name))
        call_sig = []
        param_types = []
        kw_only_emitted = False
        for param in signature.parameters.values():
            kind = "pos"
            if param.kind is inspect.Parameter.VAR_POSITIONAL:
                kind = "*args"
            elif param.kind is inspect.Parameter.VAR_KEYWORD:
                kind = "**kwargs"
            elif param.kind is inspect.Parameter.KEYWORD_ONLY:
                if not kw_only_emitted:
                    call_sig.append(
                        {
                            "name": "",
                            "kind": "kw_only",
                            "annotation": ("dyn",),
                            "default": None,
                            "has_default": False,
                        }
                    )
                    kw_only_emitted = True
            call_sig.append(
                {
                    "name": param.name,
                    "kind": kind,
                    "annotation": ("dyn",),
                    "default": None,
                    "has_default": param.default is not inspect.Parameter.empty,
                }
            )
            param_types.append(("dyn",))
        expected.append(
            {
                "name": method_name,
                "kind": "instance",
                "return_ty": ("dyn",),
                "param_types": tuple(param_types),
                "call_sig": tuple(call_sig),
                "box_int_abi": False,
            }
        )

    assert L1_CODEGEN_STATIC_METHODS == tuple(expected)


def test_runtime_source_copies_exclude_repository_build_products():
    required_ignores = ('"_native"', '"__pycache__"', '"build_*"', '"*.a.target"')
    violations = []
    for relative in _RUNTIME_SOURCE_COPY_SUITES:
        source = (ROOT / relative).read_text(encoding="utf-8")
        missing = [token for token in required_ignores if token not in source]
        if missing:
            violations.append(f"{relative}: missing {', '.join(missing)}")
    assert violations == []


def test_tests_do_not_delete_shared_runtime_archives_or_skip_under_xdist():
    forbidden = (
        "_wipe_repo_runtime_archive",
        "mutate libpy_runtime",
        'rmtree(RUNTIME / "build"',
    )
    violations = []
    for path, source in _python_test_sources():
        for token in forbidden:
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}: {token}")
        if "PYTEST_XDIST_WORKER" in source and "pytest.skip(" in source:
            violations.append(f"{path.relative_to(ROOT)}: worker-specific pytest skip")
    assert violations == []


def test_tests_use_only_the_content_addressed_pcc_python_runtime():
    """The C runtime is retired: no test builds, links or names its archives.

    Native probes consume the immutable content-addressed pcc-Python fixtures.
    """

    retired = re.compile(
        r"\b(?:libpy_runtime|libpy_runtime_pcc|libpy_runtime_libpython)\.a\b"
        r"|\b(?:threaded_)?c_runtime_archive\b"
        r"|\bcached_(?:threaded_)?c_runtime\b"
    )
    violations = [
        f"{path.relative_to(ROOT)}: {match.group(0)}"
        for path, source in _python_test_sources()
        for match in retired.finditer(source)
    ]
    assert violations == []

    # One definition, in the root conftest, visible to every test tree.
    fixtures = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    python_fixtures = (ROOT / "tests/python/conftest.py").read_text(encoding="utf-8")
    cache = (ROOT / "tests/runtime_build_cache.py").read_text(encoding="utf-8")
    assert "def pcc_runtime_archive" in fixtures
    assert "def threaded_pcc_runtime_archive" in fixtures
    assert "def pcc_runtime_archive" not in python_fixtures
    assert "def threaded_pcc_runtime_archive" not in python_fixtures
    assert "def cached_pcc_python_runtime" in cache
    assert "def cached_threaded_pcc_python_runtime" in cache
    assert cache.count('f"PCC_WITH_THREADS={1 if threaded else 0}"') == 1
    assert '_PCC_RUNTIME_CACHE_MARKER_SCHEMA = "pcc.runtime-build-cache.v4"' in cache
    for source in (fixtures, python_fixtures):
        assert "_runtime_archive_stale" not in source
        assert "_PCC_PY_RUNTIME_ARCHIVE" not in source
    assert "fcntl.flock" in cache
    assert "os.replace(work_runtime, runtime)" in cache


def test_default_xdist_keeps_bounded_workers_for_compiler_heavy_suite():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "-n 6 --dist=loadgroup -m 'not integration'" in config
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert "def pytest_xdist_auto_num_workers" not in conftest
    assert "from tests.xdist_cpu_budget import (" in conftest
    assert "items[:] = warmup + remaining" in conftest
    matrix = (ROOT / "tests/python/test_gc_backend_under_env.py").read_text(
        encoding="utf-8"
    )
    # Only the self backend remains (983254526): the matrix groups its
    # slices as pcc_heavy_self and the full default slice as
    # pcc_heavy_self_full.
    assert 'name="pcc_heavy_self_full"' in matrix
    assert 'else "pcc_heavy_self"' in matrix


def test_boc_speedup_proofs_share_one_measured_performance_lane():
    from tests.python import test_boc_benchmarks as ring
    from tests.python import test_boc_threading_proof as bank

    assert ring.MIN_RING_SPEEDUP == 1.5
    assert bank.MIN_SPEEDUP == 2.5
    assert ring.pytestmark.mark.kwargs["name"] == "pcc_heavy_llvm"
    assert bank.pytestmark.mark.kwargs["name"] == "pcc_heavy_llvm"
    assert callable(ring.test_boc_ring_correctness)
    assert callable(bank.test_pcc_threads_complete_all_workers)

    ring_gate = next(
        mark
        for mark in ring.test_boc_ring_correctness_and_speedup.pytestmark
        if mark.name == "pcc_gate"
    )
    bank_gate = next(
        mark
        for mark in bank.test_pcc_threads_give_real_parallel_speedup.pytestmark
        if mark.name == "pcc_gate"
    )
    assert ring_gate.kwargs["env"] == "PCC_RUN_BOC_SPEEDUP"
    assert bank_gate.kwargs["env"] == "PCC_RUN_BOC_SPEEDUP"


def test_lock_owned_self_host_warmer_has_independent_inner_budget(monkeypatch):
    from tests.python import test_self_host_oracle_diff as oracle

    monkeypatch.setenv("PCC_OUTER_PARALLELISM", "6")
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    assert oracle._child_env()["PCC_PY_FRONTEND_JOBS"] == "4"

    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "3")
    assert oracle._child_env()["PCC_PY_FRONTEND_JOBS"] == "3"


def test_gc_meta_matrix_retains_required_modes_without_accidental_duplicates():
    from tests.python import test_gc_backend_under_env as matrix
    from tests.python import test_runtime_oracle_diff as runtime_oracle
    from tests.python import test_self_host_oracle_diff as self_host_oracle

    for target in matrix._FRONTEND_INDEPENDENT_TARGETS:
        assert matrix._self_frontend_target(target) is None
    assert (
        matrix._self_frontend_target(matrix._CONCURRENT_COLLECTION_FILE)
        == matrix._CONCURRENT_COLLECTION_PCC_PYTHON_TARGET
    )
    assert len(matrix._self_frontend_target(matrix._GENERATIONAL_TARGETS)) < len(
        matrix._GENERATIONAL_TARGETS
    )
    assert len(matrix._self_frontend_target(matrix._RELOCATING_TARGETS)) < len(
        matrix._RELOCATING_TARGETS
    )

    cases = {
        (frontend, gc_backend): set(matrix._target_args(targets))
        for parameter in matrix._iter_cases()
        for frontend, gc_backend, targets in (parameter.values,)
    }
    heavy_groups = {
        (frontend, gc_backend): next(
            mark.kwargs["name"]
            for mark in parameter.marks
            if mark.name == "xdist_group"
        )
        for parameter in matrix._iter_cases()
        for frontend, gc_backend, _targets in (parameter.values,)
    }
    gc4_contract = "tests/python/test_gc_backend4_production.py"
    assert gc4_contract in cases[(None, "4")]
    assert gc4_contract in cases[("self", "4")]
    assert set(heavy_groups.values()) == {
        "pcc_heavy_self_full",
        "pcc_heavy_self",
    }
    assert all(
        group == ("pcc_heavy_self_full" if frontend is None else "pcc_heavy_self")
        for (frontend, _gc_backend), group in heavy_groups.items()
    )
    assert runtime_oracle.pytestmark.mark.kwargs["name"] == "pcc_heavy_self"
    assert self_host_oracle.pytestmark.mark.kwargs["name"] == "pcc_heavy_self"

    for gc_backend, targets in matrix._BACKEND_TEST_GROUPS.items():
        complete = {
            node for target in targets for node in matrix._target_args(target)
        }
        assert cases[(None, gc_backend)] == complete
        assert all(
            node in complete or node.split("::", 1)[0] in complete
            for node in cases[("self", gc_backend)]
        )


def test_gc_meta_matrix_uses_owned_backend_and_stops_at_first_failure(monkeypatch):
    from tests.python import test_gc_backend_under_env as matrix

    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(kwargs["args"], 0, stdout="", stderr="")

    monkeypatch.setenv("PCC_BACKEND", "llvm")
    monkeypatch.setattr(matrix.subprocess, "run", run)
    for backend in matrix._FRONTEND_BACKENDS:
        matrix._run_file_under_backends(backend, "4", "unchanged-test-target.py")
    assert [call["env"]["PCC_BACKEND"] for call in calls] == ["", "self"]
    assert all("-x" in call["args"] for call in calls)
    assert all(call["args"][-1] == "unchanged-test-target.py" for call in calls)


def test_stateless_gc_compile_suites_are_not_forced_onto_one_worker():
    for relative in _STATELESS_GC_COMPILE_SUITES:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "xdist_group" not in source, relative


def test_csmith_seeds_remain_independently_schedulable():
    source = (ROOT / "tests/c/test_csmith.py").read_text(encoding="utf-8")
    assert "xdist_group" not in source
    # flat per-seed parametrize over the vetted corpus — one xdist item per
    # seed, never grouped (corpus rationale lives in test_csmith.py)
    assert (
        '@pytest.mark.parametrize("seed", CSMITH_SEED_CORPUS[:DEFAULT_SEEDS])'
        in source
    )


def test_heavy_external_c_corpora_are_in_the_integration_gate():
    for relative in _HEAVY_EXTERNAL_CORPUS_SUITES:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "pytestmark = pytest.mark.integration" in source, relative

    promotion = (ROOT / "scripts/run_self_backend_promotion_gate.py").read_text(
        encoding="utf-8"
    )
    for relative in (
        "tests/c/test_c_testsuite_self.py",
        "tests/c/test_gcc_torture_self.py",
    ):
        start = promotion.index(relative) - 80
        assert '"-m", "integration"' in promotion[start : start + 160]


def test_full_three_stage_gc_bootstraps_are_in_the_integration_gate():
    for relative in _FULL_BOOTSTRAP_INTEGRATION_GATES:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "pytestmark = pytest.mark.integration" in source, relative

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    commands = [
        line
        for line in agents.splitlines()
        if "test_pcc_bootstrap_full_gc" in line and "pytest" in line
    ]
    assert commands
    assert all("-m integration" in line for line in commands)


def test_gc_root_graph_fixture_is_not_rebuilt_by_every_xdist_worker():
    source = (
        ROOT / "tests/python/gc_production_contract/test_root_graphs.py"
    ).read_text(encoding="utf-8")
    assert 'xdist_group(name="gc_root_graphs")' in source


def test_fallback_closure_fixture_is_not_rebuilt_by_every_xdist_worker():
    source = (ROOT / "tests/python/test_fallback_baseline.py").read_text(
        encoding="utf-8"
    )
    assert 'xdist_group(name="fallback_baseline")' in source


def test_pcc1_threaded_runtime_fixture_is_not_rebuilt_by_every_xdist_worker():
    source = (ROOT / "tests/python/test_pcc1_threading_gc_runtime.py").read_text(
        encoding="utf-8"
    )
    assert 'xdist_group(name="pcc1_threaded_gc")' in source


def test_self_host_oracle_stages_are_shared_across_xdist_workers():
    source = (ROOT / "tests/python/test_self_host_oracle_diff.py").read_text(
        encoding="utf-8"
    )
    cache = (ROOT / "tests/runtime_build_cache.py").read_text(encoding="utf-8")
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert "cached_self_host_oracle_dir()" in source
    assert "self_host_source_key()" in source
    assert "self_backend_object_cache_key()" in source
    assert "PCC_PY_FRONTEND_IR_CACHE_IDENTITY" in source
    assert "PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY" in source
    assert "fcntl.flock" in source
    assert "os.replace(temporary, pcc1)" in source
    assert "os.replace(temporary, pcc2)" in source
    assert "os.replace(temporary, pcc3)" in source
    assert "pcc.self-host-test-artifact.v2" in cache
    assert "self-host-oracle" in cache
    assert "test_000_self_host_oracle_stage_cache_warmup" in conftest


def test_uv_locked_wheel_bundles_a_current_source_pcc1():
    source = (ROOT / "tests/integration/test_uv_locked_pcc_sync.py").read_text(
        encoding="utf-8"
    )

    assert "find_current_pcc1(REPO)" in source
    assert 'REPO / "build" / "bootstrap" / "pcc1"' not in source
