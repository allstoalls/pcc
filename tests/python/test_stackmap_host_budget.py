"""Pure host guard/model routing tests; no compiler or native execution."""

import sys
from types import SimpleNamespace

import pytest

from pcc.backend import stackmap_host_budget as host
from pcc.backend.precise_stackmap import PreciseStackMapError
from pcc.frontends.python import worker_resource_plan


def configured_host(monkeypatch, *, raw="100000", observation=None):
    environment = {"PCC_WORKER_TREE_BUDGET_BYTES": raw,
                   "PCC_WORKER_TREE_STATE_PATH": "unused-test-state"}
    runtime = SimpleNamespace(implementation=SimpleNamespace(name="cpython"),
                              version_info=(3, 13, 0), platform="darwin",
                              getsizeof=sys.getsizeof, _is_gil_enabled=lambda: True)
    monkeypatch.setattr(host, "sys", runtime)
    monkeypatch.setattr(host, "os", SimpleNamespace(environ=environment, getpid=lambda: 41))
    monkeypatch.setattr(host, "struct", SimpleNamespace(calcsize=lambda _format: 8))
    monkeypatch.setitem(sys.modules, "sysconfig", SimpleNamespace(get_config_var=lambda _name: 0))
    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        RUSAGE_SELF=0, getrusage=lambda _who: SimpleNamespace(ru_maxrss=10000),
    ))
    observed = (1000, {41: 9000}) if observation is None else observation
    monkeypatch.setattr(worker_resource_plan, "read_tree_state", lambda *args, **kwargs: observed)
    return environment, runtime


def test_guard_cap_is_estimate_and_original_work_bound_is_derived(monkeypatch):
    configured_host(monkeypatch)
    budget = host.host_compact_stackmap_budget()
    assert budget.limit_bytes == 89000
    assert budget.record_visit_limit == 3 * (89000 // 32)
    assert budget.retained_bytes == 0
    assert budget.integer_bytes >= sys.getsizeof((1 << 64) - 1)
    assert budget.integer_bytes % 16 == 0


@pytest.mark.parametrize("raw", ["-1", "0", "true", str(1 << 63)])
def test_bad_existing_cap_is_rejected(monkeypatch, raw):
    configured_host(monkeypatch, raw=raw)
    with pytest.raises(PreciseStackMapError, match="existing stack-map tree budget"):
        host.host_compact_stackmap_budget()


@pytest.mark.parametrize("missing", ["PCC_WORKER_TREE_BUDGET_BYTES", "PCC_WORKER_TREE_STATE_PATH"])
def test_missing_guard_keeps_legacy_route(monkeypatch, missing):
    environment, _runtime = configured_host(monkeypatch)
    del environment[missing]
    assert host.host_compact_stackmap_budget() is None


def test_unavailable_guard_state_never_means_zero_rss(monkeypatch):
    configured_host(monkeypatch)
    monkeypatch.setattr(worker_resource_plan, "read_tree_state", lambda *args, **kwargs: None)
    assert host.host_compact_stackmap_budget() is None


@pytest.mark.parametrize("mode", ["native", "old-version", "free-threaded", "debug", "pymalloc_debug", "malloc_debug", "mimalloc_debug"])
def test_unsupported_model_keeps_legacy_route(monkeypatch, mode):
    environment, runtime = configured_host(monkeypatch)
    if mode == "native":
        runtime.implementation.name = "pcc"
    elif mode == "old-version":
        runtime.version_info = (3, 12, 0)
    elif mode == "free-threaded":
        runtime._is_gil_enabled = lambda: False
    else:
        environment["PYTHONMALLOC"] = mode
    assert host.host_compact_stackmap_budget() is None


def test_exhausted_estimate_does_not_expand_guard(monkeypatch):
    configured_host(monkeypatch, raw="10000")
    assert host.host_compact_stackmap_budget() is None


def test_host_budget_provider_is_excluded_from_native_closure():
    from pathlib import Path
    from pcc.frontends.python import pipeline_self_backend_link as link
    from pcc.frontends.python.pipeline_import_scan import (
        _without_inactive_implementation_imports,
    )
    source = Path(link.__file__).read_text()
    marker = "from pcc.backend.stackmap_host_budget import"
    assert marker in source
    assert marker not in _without_inactive_implementation_imports(source, implementation="pcc")
    assert marker in _without_inactive_implementation_imports(source, implementation="cpython")


@pytest.mark.parametrize("build_flag", [1, None])
def test_free_threaded_or_unverified_build_rejects_even_with_gil_enabled(monkeypatch, build_flag):
    configured_host(monkeypatch)
    monkeypatch.setitem(sys.modules, "sysconfig", SimpleNamespace(get_config_var=lambda _name: build_flag))
    assert host.host_compact_stackmap_budget() is None
