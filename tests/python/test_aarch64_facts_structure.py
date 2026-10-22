"""Bounded facts observation controls; fake CFGs, no profiler or native execution."""

import ast
import gc
import json
from pathlib import Path
import weakref

import pytest

from scripts import host_assembly_profile as adapter
from tests.python.test_aarch64_regalloc_single_seed import (
    FakeKernel, ORACLE, SOURCE, case, evaluate, load_facts,
)


def _run(blocks, observer=None):
    function, counts = load_facts(SOURCE, count=True)
    function.__globals__["_HOST_FACTS_STRUCTURE_OBSERVER"] = observer
    kernel = FakeKernel(blocks)
    result = evaluate(function, kernel)
    return result, kernel.events, counts


def _sample(observer, *, B=3, homes=None, uses=None, rows=None):
    return observer.begin({1: 0, 2: 0} if homes is None else homes,
                          {1: [1], 2: [2]} if uses is None else uses,
                          [[], [0], [0]] if rows is None else rows,
                          list(range(B)), list(range(B)))


@pytest.mark.parametrize("overlap", [False, True])
def test_observed_actual_walk_counts_and_seven_outputs(overlap):
    blocks = (case([[1], [2], [3], []], {2: [1, 2], 3: [1, 2]}, [(1, 0), (2, 0)])
              if overlap else case([[1, 2], [], []], {1: [1], 2: [2]}, [(1, 0), (2, 0)]))
    observer = adapter._FactsStructureObserver()
    result, events, counts = _run(blocks, observer)
    plain, plain_events, plain_counts = _run(blocks)
    old = load_facts(ORACLE)[0]
    old_kernel = FakeKernel(blocks)
    assert result == plain == evaluate(old, old_kernel)
    assert events == plain_events == old_kernel.events
    assert counts == plain_counts
    row = observer.report()["details"][0]
    expected = (6, 6, 3, 3) if overlap else (2, 2, 2, 2)
    assert tuple(row[key] for key in ("P", "Q", "U", "E_seen")) == expected
    assert row["walking_queries"] == 2 and row["memo_hits"] == 0
    assert row["seed_active"] == 2 and row["home"] == 0
    assert observer.active is None
    assert all(type(value) in (int, float, str, type(None)) for value in row.values())


def test_single_seed_memo_drain_is_not_an_observed_walk_pop():
    blocks = case([[1], [2], []], {2: [1, 2, 3]}, [(1, 0), (2, 0), (3, 0)])
    observer = adapter._FactsStructureObserver()
    _, _, counts = _run(blocks, observer)
    row = observer.details[0]
    assert (row["seed_active"], row["walking_queries"], row["memo_hits"], row["P"]) == (3, 1, 2, 2)
    assert counts["pops"] == 2 and counts["hits"] == 2


@pytest.mark.parametrize("homes,uses,selected,active,traffic", [
    ({1: 0, 2: 0, 3: 1, 4: 1}, {1: [2], 2: [2], 3: [2], 4: [2]}, 0, 2, 2),
    ({1: 0, 2: 0, 3: 1, 4: 1}, {1: [2], 2: [2], 3: [2, 2], 4: [2]}, 1, 2, 3),
    ({1: 0, 2: 0, 3: 1, 4: 1, 5: 0}, {1: [2], 2: [2], 3: [2, 2], 4: [2], 5: [2]}, 0, 3, 3),
    ({}, {1: [1], 2: [2]}, -1, 2, 2),
    ({1: 0, 2: 0, 3: 0}, {1: [0], 2: [1], 3: [2]}, 0, 2, 2),
])
def test_selection_counts_raw_duplicates_and_uses_stable_ties(homes, uses, selected, active, traffic):
    observer = adapter._FactsStructureObserver()
    sample = _sample(observer, homes=homes, uses=uses)
    assert (sample["home"], sample["seed_active"], sample["seed_traffic"]) == (selected, active, traffic)
    observer.complete(sample)
    assert sample["seen"] is None


@pytest.mark.parametrize("variant", ["minus_one", "custom_value", "custom_seed", "negative_seed", "large_seed", "length", "custom_row", "home_only"])
def test_noncanonical_or_ineligible_census_falls_back(variant):
    class CustomInt(int):
        pass
    observer = adapter._FactsStructureObserver()
    homes, uses, rows, bases, ends = {1: 0, 2: 0}, {1: [1], 2: [2]}, [[], [0], [0]], [0, 1, 2], [0, 1, 2]
    if variant == "minus_one":
        uses[-1] = [1]
    elif variant == "custom_value":
        uses[CustomInt(3)] = [1]
    elif variant == "custom_seed":
        uses[1] = [CustomInt(1)]
    elif variant == "negative_seed":
        uses[1] = [-1]
    elif variant == "large_seed":
        uses[1] = [3]
    elif variant == "length":
        bases.pop()
    elif variant == "custom_row":
        rows[1] = (0,)
    else:
        uses = {1: [0], 2: [0]}
    assert observer.begin(homes, uses, rows, bases, ends) is None
    assert observer.active is None and not observer.details


@pytest.mark.parametrize("cap", ["FACTS_BLOCK_CAP", "FACTS_CENSUS_CAP", "FACTS_INITIALIZED_CAP", "FACTS_CALL_CAP"])
def test_census_caps_before_excess_and_shared_caps_never_resume(monkeypatch, cap):
    observer = adapter._FactsStructureObserver()
    monkeypatch.setattr(adapter, cap, 0)
    assert _sample(observer) is None
    assert observer.active is None and not observer.details
    if cap != "FACTS_BLOCK_CAP":
        assert observer.disabled is not None
        monkeypatch.setattr(adapter, cap, 100)
        assert _sample(observer) is None


def test_exact_census_and_initialized_boundaries(monkeypatch):
    baseline = adapter._FactsStructureObserver()
    sample = _sample(baseline)
    required = baseline.census
    baseline.complete(sample)
    monkeypatch.setattr(adapter, "FACTS_CENSUS_CAP", required)
    monkeypatch.setattr(adapter, "FACTS_INITIALIZED_CAP", 3)
    observer = adapter._FactsStructureObserver()
    sample = _sample(observer)
    assert sample is not None and observer.census == required
    observer.complete(sample)
    assert _sample(observer) is None and observer.disabled is not None


def test_call_cap_censors_only_current_call_and_releases_bitmap():
    observer = adapter._FactsStructureObserver()
    sample = _sample(observer)
    assert observer.query(sample, 0, True, False)
    for index in range(24):
        assert observer.row(sample, 1, 1) is (index < 23)
    assert sample["censored"] == "call_pop_cap" and sample["seen"] is None
    assert not observer.query(sample, 0, True, False)
    observer.complete(sample)
    assert observer.disabled is None and _sample(observer) is not None


def test_global_pop_cap_midquery_finishes_business_once_and_never_resumes(monkeypatch):
    monkeypatch.setattr(adapter, "FACTS_POP_CAP", 1)
    blocks = case([[1], [2], [3], []], {2: [1, 2], 3: [1, 2]}, [(1, 0), (2, 0)])
    observer = adapter._FactsStructureObserver()
    result, events, counts = _run(blocks, observer)
    expected, old_events, old_counts = _run(blocks)
    assert (result, events, counts) == (expected, old_events, old_counts)
    row = observer.details[0]
    assert (row["P"], row["Q"], row["U"], row["E_seen"]) == (1, 1, 1, 1)
    assert row["censored"] == "global_pop_cap" and observer.active is None
    assert _sample(observer) is None


@pytest.mark.parametrize("where", ["begin", "query", "row", "complete"])
def test_observer_baseexception_never_drops_or_replays_business(where):
    class Failed(adapter._FactsStructureObserver):
        pass
    def fail(self, *args):
        # Simulate failure after partial P/Q mutation as well as before it.
        if where == "row":
            args[0]["P"] += 1
        raise BaseException("observer")
    setattr(Failed, where, fail)
    blocks = case([[1], [2], [3], []], {2: [1, 2], 3: [1, 2]}, [(1, 0), (2, 0)])
    observer = Failed()
    assert _run(blocks, observer) == _run(blocks)
    assert observer.active is None and not observer.details
    assert observer.totals["P"] == observer.totals["Q"] == 0
    assert observer.disabled is not None


def test_business_baseexception_identity_and_no_kernel_retention():
    error = BaseException("business")
    class Raising(FakeKernel):
        def terminator_header(self, block):
            raise error
    function, _ = load_facts(SOURCE)
    observer = adapter._FactsStructureObserver()
    function.__globals__["_HOST_FACTS_STRUCTURE_OBSERVER"] = observer
    kernel = Raising(case([[]]))
    with pytest.raises(BaseException) as raised:
        function(kernel)
    assert raised.value is error and observer.active is None
    normal = FakeKernel(case([[1], [2], []], {2: [1, 2]}, [(1, 0), (2, 0)]))
    reference = weakref.ref(normal)
    function(normal)
    del normal
    gc.collect()
    assert reference() is None and observer.active is None


def test_report_uses_only_bounded_scalar_rows_and_aggregates():
    observer = adapter._FactsStructureObserver()
    for _ in range(adapter.FACTS_DETAIL_CAP + 3):
        sample = _sample(observer)
        observer.row(sample, 1, 1)
        observer.complete(sample)
    report = observer.report()
    assert len(report["details"]) == adapter.FACTS_DETAIL_CAP and report["details_omitted"] == 3
    assert report["totals"]["P"] == adapter.FACTS_DETAIL_CAP + 3
    assert len(json.dumps(report, ensure_ascii=True).encode("ascii")) < adapter.REPORT_LIMIT // 2
    assert all("seen" not in row for row in report["details"])


def test_all_observer_references_are_under_adjacent_standalone_cpython_guards():
    tree = ast.parse(SOURCE.read_text())
    guarded = set()
    for parent in ast.walk(tree):
        for _, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            for index, node in enumerate(value):
                if (isinstance(node, ast.If)
                        and ast.unparse(node.test) == "sys.implementation.name == 'cpython'"):
                    assert index and isinstance(value[index - 1], ast.Import)
                    assert ast.unparse(value[index - 1]) == "import sys"
                    guarded.update(child for item in node.body for child in ast.walk(item))
    references = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and (node.id.startswith("_facts_") or node.id == "_HOST_FACTS_STRUCTURE_OBSERVER")]
    assert references and all(node in guarded for node in references)
    assert {"_HOST_FACTS_STRUCTURE_OBSERVER", "_facts_sample", "_facts_observer"} <= {node.id for node in references}


def test_business_row_exception_keeps_identity_and_invalidates_uncommitted_sample():
    error = BaseException("row business")
    class RaisingRow(list):
        def __iter__(self):
            raise error
    class Inject(adapter._FactsStructureObserver):
        def begin(self, definitions, uses, rows, bases, ends):
            sample = super().begin(definitions, uses, rows, bases, ends)
            # Test-only failure injection after the exact-container census.
            rows[2] = RaisingRow(rows[2])
            return sample
    observer = Inject()
    function, _ = load_facts(SOURCE)
    function.__globals__["_HOST_FACTS_STRUCTURE_OBSERVER"] = observer
    kernel = FakeKernel(case([[1], [2], []], {2: [1, 2]}, [(1, 0), (2, 0)]))
    with pytest.raises(BaseException) as raised:
        function(kernel)
    assert raised.value is error
    sample = observer.active
    assert sample is not None and observer.completed == 0 and not observer.details
    # This is the adapter's backend-finally invalidation boundary.
    adapter._invalidate_facts(observer, "facts_or_backend_raised")
    assert observer.active is None and sample["seen"] is None and observer.invalid == 1
    assert observer.totals["P"] == observer.totals["Q"] == 0


@pytest.mark.parametrize("mode", ["disabled", "begin_none", "query_error", "row_error", "cap"])
def test_disabled_and_stopped_calls_pay_only_local_per_value_predicate(monkeypatch, mode):
    import builtins
    observer = adapter._FactsStructureObserver()
    if mode == "disabled":
        observer = None
    elif mode == "begin_none":
        monkeypatch.setattr(observer, "begin", lambda *args: None)
    elif mode in ("query_error", "row_error"):
        def failed(*args):
            raise BaseException("observer")
        monkeypatch.setattr(observer, mode.split("_")[0], failed)
    else:
        monkeypatch.setattr(adapter, "FACTS_POP_CAP", 1)
    blocks = case([[1], [2], [3], []], {2: [1, 2, 3], 3: [1, 2, 3]}, [(1, 0), (2, 0), (3, 0)])
    function, _ = load_facts(SOURCE)
    function.__globals__["_HOST_FACTS_STRUCTURE_OBSERVER"] = observer
    original_import = builtins.__import__
    imports = []
    def counted(name, *args, **kwargs):
        if name == "sys":
            imports.append(name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", counted)
    function(FakeKernel(blocks))
    # Initial readiness and final completion guards are once per facts call.
    # Only the first enabled value pays the additional nested native guard.
    assert len(imports) == (2 if mode in ("disabled", "begin_none") else 3)


def test_counter_width_is_checked_before_block_sized_allocation(monkeypatch):
    import sys
    from types import SimpleNamespace
    class Wide:
        itemsize = 8
        def __imul__(self, count):
            pytest.fail("allocated before validating the counter width")
    monkeypatch.setitem(sys.modules, "array", SimpleNamespace(array=lambda *args: Wide()))
    observer = adapter._FactsStructureObserver()
    assert _sample(observer) is None and observer.disabled == "counter_width"


def _counting_helper():
    # Extract only this helper; importing scan_reference would import PCC.
    import inspect
    source = (SOURCE.parents[2] / "tests/aarch64_regalloc_scan_reference.py").read_text()
    helper = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                  and node.name == "counted_function_facts")
    namespace = dict(ast=ast, inspect=inspect)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                 "<finite-facts-counting-helper>", "exec"), namespace)
    return namespace["counted_function_facts"]


@pytest.mark.parametrize("path", [SOURCE, ORACLE])
def test_shared_counting_helper_handles_exact_production_and_oracle_bodies(path):
    function, _ = load_facts(path)
    counts = dict(pops=0, edges=0, hits=0)
    counted = _counting_helper()(function, counts)
    blocks = case([[1], [2], []], {2: [1, 2, 3]}, [(1, 0), (2, 0), (3, 0)])
    assert evaluate(counted, FakeKernel(blocks)) == evaluate(function, FakeKernel(blocks))
    assert counts == (dict(pops=2, edges=2, hits=2) if path == SOURCE else dict(pops=6, edges=6, hits=0))


@pytest.mark.parametrize("change", ["remove_prefix", "mutate_business", "add_loop"])
def test_shared_counting_helper_rejects_changed_loop_contract(change):
    from types import SimpleNamespace
    function, _ = load_facts(SOURCE)
    tree = ast.parse(SOURCE.read_text())
    definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_function_level_facts")
    loops = [node for node in ast.walk(definition) if isinstance(node, ast.While)
             and isinstance(node.test, ast.Name) and node.test.id == "pending"]
    observed = next(node for node in loops if len(node.body) == 6)
    if change == "remove_prefix":
        observed.test = ast.Constant(False)
    elif change == "mutate_business":
        observed.body[1].value = ast.Constant(991)
    else:
        definition.body.append(ast.parse("while pending:\n    pending.pop()\n").body[0])
    changed = ast.unparse(ast.fix_missing_locations(definition))
    helper = _counting_helper()
    original_inspect = helper.__globals__["inspect"]
    helper.__globals__["inspect"] = SimpleNamespace(
        getsource=lambda target: changed if target is function else original_inspect.getsource(target))
    with pytest.raises(AssertionError):
        helper(function, dict(pops=0, edges=0, hits=0))


def test_completion_failure_keeps_prior_publication_transactional():
    observer = adapter._FactsStructureObserver()
    first = _sample(observer)
    observer.row(first, 1, 1)
    observer.complete(first)
    previous = (list(observer.details), dict(observer.totals), observer.completed)
    class FailedItems(dict):
        def items(self):
            for index, pair in enumerate(super().items()):
                if index == 1:
                    raise BaseException("completion preparation")
                yield pair
    observer.totals = FailedItems(observer.totals)
    blocks = case([[1], [2], []], {2: [1, 2]}, [(1, 0), (2, 0)])
    assert _run(blocks, observer) == _run(blocks)
    assert (observer.details, dict(observer.totals), observer.completed) == previous
    assert observer.active is None and observer.invalid == 1
    assert observer.disabled == "observer_error"
