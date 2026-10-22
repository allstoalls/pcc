"""Focused width proofs for existing GC step/pause telemetry.

Host tests execute complete source-extracted production functions with exact
signed 32/64-bit memory doubles and inert collector/configuration providers.
The ELF test uses host pcc, its owned self emitter, assembler and linker; it
executes production pause/increment bodies and source-derived telemetry/reset
slices against the real state object. Only legacy ``int`` annotations in the
managed helpers are normalized to ``i64`` for that freestanding slice, and
the two private helpers receive test-only C-ABI export decorators. Their
production bodies are unchanged.

This is neither a full-runtime/five-GC integration gate nor native-pcc1,
concurrency, scheduler-policy, timing-accuracy or performance evidence.
"""
from __future__ import annotations

import ast
import platform
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_I32_GLOBALS,
    FREESTANDING_GC_I64_GLOBALS,
)


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py"
STATE = RUNTIME / "freestanding_gc_state.py"
BACKEND = RUNTIME / "py_gc_backend.py"
SCHEDULER = RUNTIME / "freestanding_gc_incremental_concurrent_scheduler.py"
DISPATCHER = RUNTIME / "freestanding_gc_barrier_dispatcher.py"
MAPPED_ROOTS = RUNTIME / "freestanding_gc_mapped_roots.py"
TELEMETRY = RUNTIME / "py_gc_telemetry.py"
WIDE_METRICS = {
    "pcc_gc_metric_step": (5, "WORK_STEPS"),
    "pcc_gc_metric_max_pause_us": (7, "MAX_PAUSE_US"),
    "pcc_gc_metric_pause_count": (32, "PAUSE_COUNT"),
    "pcc_gc_metric_pause_sum_us": (33, "PAUSE_SUM_US"),
}
NARROW_COUNTERS = (
    "pcc_gc_metric_alloc",
    "pcc_gc_metric_store",
    "pcc_gc_metric_load",
    "pcc_gc_metric_safepoint",
    "pcc_gc_metric_pin",
)
HISTOGRAM = tuple(f"pcc_gc_metric_pause_hist{i}" for i in range(4))


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(path, name):
    matches = [
        node for node in _tree(path).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1, (path, name)
    return matches[0]


def _global_name(node):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "global_addr"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
    ):
        return node.args[0].value
    return None


def _definitions():
    definitions = {}
    for statement in _tree(STATE).body:
        if not isinstance(statement, ast.Expr):
            continue
        call = statement.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id not in ("define_global_i32", "define_global_i64"):
            continue
        name = ast.literal_eval(call.args[0])
        assert name not in definitions
        definitions[name] = (call.func.id, ast.literal_eval(call.args[1]))
    return definitions


def test_phase_counter_definitions_registry_and_metric_ids_agree():
    definitions = _definitions()
    header = (ROOT / "pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    for name, (metric, public_name) in WIDE_METRICS.items():
        assert definitions[name] == ("define_global_i64", 0)
        assert name in FREESTANDING_GC_I64_GLOBALS
        assert name not in FREESTANDING_GC_I32_GLOBALS
        assert re.search(rf"\bPCC_GC_COUNTER_{public_name}\s*=\s*{metric}\s*,", header)
    for name in NARROW_COUNTERS + HISTOGRAM:
        assert definitions[name] == ("define_global_i32", 0)
        assert name in FREESTANDING_GC_I32_GLOBALS
        assert name not in FREESTANDING_GC_I64_GLOBALS


def test_phase_counter_direct_accesses_use_declared_width():
    accesses = {name: set() for name in WIDE_METRICS}
    for path in (BACKEND, SCHEDULER, DISPATCHER, TELEMETRY):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ("load_i32", "load_i64", "store_i32", "store_i64"):
                continue
            name = _global_name(node.args[0])
            if name not in accesses:
                continue
            assert node.func.id in ("load_i64", "store_i64"), (
                path.name, node.lineno, name, node.func.id,
            )
            accesses[name].add(node.func.id)
    assert all(kinds == {"load_i64", "store_i64"} for kinds in accesses.values())


class _Memory:
    """Exact signed loads and truncating stores, with declared object bounds."""

    def __init__(self):
        # Reset also clears a few registered counters owned by other modules.
        # Their initial values are irrelevant here; retain their ABI widths.
        self.cells = {name: bytearray(4) for name in FREESTANDING_GC_I32_GLOBALS}
        self.cells.update({name: bytearray(8) for name in FREESTANDING_GC_I64_GLOBALS})
        for name, (kind, initial) in _definitions().items():
            width = 8 if kind == "define_global_i64" else 4
            self.cells[name] = bytearray(width)
            self.store(name, 0, initial, width)

    def load(self, name, offset, width):
        cell = self.cells[name]
        assert 0 <= offset <= len(cell) - width, (name, offset, width)
        return int.from_bytes(cell[offset:offset + width], "little", signed=True)

    def store(self, name, offset, value, width):
        cell = self.cells[name]
        assert 0 <= offset <= len(cell) - width, (name, offset, width)
        encoded = (value & ((1 << (width * 8)) - 1)).to_bytes(width, "little")
        cell[offset:offset + width] = encoded

    def get(self, name):
        return self.load(name, 0, len(self.cells[name]))

    def set(self, name, value):
        self.store(name, 0, value, len(self.cells[name]))


def _host_functions(*, other_mutators=()):
    memory = _Memory()
    noop = lambda *args: None
    # Model the registered records consumed by the thread-kernel providers:
    # the caller is live, and even an exited (blocking) thread stays registered.
    # Only another mutator parked at an application gate permits relocation;
    # a runtime park, running thread or exited thread is not sufficient.
    namespace = {
        "i64": int,
        "global_addr": lambda name: name,
        "load_i32": lambda name, offset: memory.load(name, offset, 4),
        "load_i64": lambda name, offset: memory.load(name, offset, 8),
        "store_i32": lambda name, offset, value: memory.store(name, offset, value, 4),
        "store_i64": lambda name, offset, value: memory.store(name, offset, value, 8),
        "_atomic_i32_store": lambda name, value: memory.store(name, 0, value, 4),
        "gc_backend_current": lambda: memory.get("pcc_gc_backend_selected"),
        "_selected_backend": lambda: memory.get("pcc_gc_backend_selected"),
        "pcc_thread_live_count": lambda: 1 + len(other_mutators),
        "pcc_thread_all_stopped_at_gates": lambda: int(all(
            state == "parked" and at_gate for state, at_gate in other_mutators
        )),
        "_init_config": noop,
        "_object_graph_lock": noop,
        "_object_graph_unlock": noop,
        "_backend3_clear_remembered_owners": lambda: None,
        "_backend3_finish_detached_remembered_owners": noop,
        "_backend4_reset_store_buffer_epoch_state": noop,
        "_backend4_reset_remembered_set_epoch_state": noop,
        "_backend4_reseed_relocation_epoch_state": noop,
        "_backend4_clear_large_deferred_flags": noop,
        "pcc_platform_monotonic_us": lambda: 100,
        "pcc_gc_tracing_step_cycle": lambda budget: 0,
        "pcc_gc_tracing_discharge_debt": noop,
        # These width tests publish no stack map and do not opt into the
        # differential walk. The real callback is inert in that configuration.
        "pcc_gc_precise_stack_diff_if_requested": lambda: None,
        "pcc_gc_generational_step": lambda budget, automatic: 0,
        "pcc_gc_backend3_drain_promotion_worklist": lambda budget: 0,
        "pcc_gc_backend4_step_remembered_roots": lambda budget: 0,
        "pcc_gc_backend4_step_generation_aging": lambda budget: 0,
        "pcc_gc_backend4_evacuation_page_drain": lambda budget: 0,
        "pcc_gc_backend4_select_relocation_pages": lambda budget: 0,
        "_tracing_work_pending": lambda: 0,
    }
    groups = (
        (BACKEND, ("_counter_global", "_counter_inc", "pcc_gc_telemetry_reset")),
        (SCHEDULER, ("pcc_gc_tracing_record_pause", "pcc_gc_incremental_concurrent_step")),
        (DISPATCHER, ("pcc_gc_step",)),
        (TELEMETRY, ("pcc_gc_telemetry",)),
    )
    for path, names in groups:
        functions = [_function(path, name) for name in names]
        for function in functions:
            function.decorator_list = []
        module = ast.Module(body=functions, type_ignores=[])
        exec(compile(module, str(path), "exec"), namespace)
    return memory, namespace


def test_host_source_stack_diff_callback_has_declared_production_provider():
    symbol = "pcc_gc_precise_stack_diff_if_requested"
    declarations = [
        node.value for node in _tree(DISPATCHER).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == symbol
                for target in node.targets)
    ]
    assert len(declarations) == 1
    declaration = declarations[0]
    assert isinstance(declaration, ast.Call)
    assert isinstance(declaration.func, ast.Name)
    assert declaration.func.id == "extern"
    assert len(declaration.args) == 3 and not declaration.keywords
    assert ast.literal_eval(declaration.args[0]) == symbol
    assert ast.literal_eval(declaration.args[1]) == ()
    assert isinstance(declaration.args[2], ast.Name)
    assert declaration.args[2].id == "c_void"
    provider = _function(MAPPED_ROOTS, symbol)
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "c_abi_export"
        and len(decorator.args) == 1
        and ast.literal_eval(decorator.args[0]) == symbol
        for decorator in provider.decorator_list
    )


@pytest.mark.parametrize("table_present", [False, True])
@pytest.mark.parametrize("diff_requested", [False, True])
def test_host_source_stack_diff_callback_requires_table_and_opt_in(
    table_present, diff_requested,
):
    calls = []
    lookups = []
    tables = []

    def table_pointer(name):
        tables.append(name)
        return 1 if table_present else None

    def getenv(name):
        lookups.append(name)
        return 1 if diff_requested else None

    def compare(report):
        calls.append(report)
        return 0

    namespace = {
        "global_load_ptr": table_pointer,
        "ptr_is_null": lambda value: int(value is None),
        "cstr": lambda value: value,
        "pcc_platform_getenv": getenv,
        "pcc_gc_precise_stack_diff": compare,
    }
    function = _function(MAPPED_ROOTS, "pcc_gc_precise_stack_diff_if_requested")
    function.decorator_list = []
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(MAPPED_ROOTS), "exec"), namespace)
    assert namespace[function.name]() is None
    assert tables == ["pcc_gc_precise_stackmap_table"]
    assert lookups == (["PCC_GC_STACKMAP_DIFF"] if table_present else [])
    assert calls == ([1] if table_present and diff_requested else [])


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("budget", [-1, 0, 1])
@pytest.mark.parametrize("explicit_collect", [0, 1])
@pytest.mark.parametrize("remap_active", [0, 1])
def test_host_source_step_stack_diff_callback_routing(
    backend, budget, explicit_collect, remap_active,
):
    memory, functions = _host_functions()
    calls = []
    functions["pcc_gc_precise_stack_diff_if_requested"] = lambda: calls.append(1)
    memory.set("pcc_gc_backend_selected", backend)
    memory.set("pcc_gc_explicit_collect_active", explicit_collect)
    memory.set("pcc_gc_backend4_remap_active", remap_active)
    assert functions["pcc_gc_step"](budget) == 0
    automatic_step = (
        budget > 0 and not explicit_collect and backend in (0, 3, 4)
        and not (backend == 4 and remap_active)
    )
    assert calls == ([1] if automatic_step else [])


@pytest.mark.parametrize("other_mutators,may_relocate", [
    pytest.param((), True, id="caller-only"),
    pytest.param((("parked", True),), True, id="application-gate"),
    pytest.param((("parked", True), ("parked", True)), True, id="all-gated"),
    pytest.param((("running", False),), False, id="running"),
    pytest.param((("running", True),), False, id="gate-not-yet-parked"),
    pytest.param((("parked", False),), False, id="runtime-parked"),
    pytest.param((("exited", False),), False, id="exited"),
    pytest.param((("exited", True),), False, id="exited-not-parked"),
    pytest.param((("parked", True), ("parked", False)), False, id="mixed-parks"),
])
@pytest.mark.parametrize("explicit_collect", [0, 1])
def test_host_source_gc4_relocation_requires_other_mutators_at_application_gates(
    other_mutators, may_relocate, explicit_collect,
):
    memory, functions = _host_functions(other_mutators=other_mutators)
    memory.set("pcc_gc_backend_selected", 4)
    memory.set("pcc_gc_explicit_collect_active", explicit_collect)
    memory.set("pcc_gc_forwarding_population", 1)
    calls = []

    def work(name, result=0):
        def record(*args):
            calls.append((name, args))
            return result
        return record

    for symbol in (
        "pcc_gc_backend4_step_remembered_roots",
        "pcc_gc_backend4_step_generation_aging",
        "pcc_gc_backend4_evacuation_page_drain",
        "pcc_gc_backend4_select_relocation_pages",
        "pcc_gc_backend3_drain_promotion_worklist",
        "pcc_gc_tracing_step_cycle",
    ):
        functions[symbol] = work(symbol)
    retire = "pcc_gc_backend4_remap_and_retire_stopped_world"
    functions[retire] = work(retire, 1)

    assert functions["pcc_thread_live_count"]() == 1 + len(other_mutators)
    assert functions["pcc_thread_all_stopped_at_gates"]() == int(may_relocate)
    assert functions["pcc_gc_step"](7) == int(may_relocate)
    if explicit_collect:
        expected = [
            ("pcc_gc_backend3_drain_promotion_worklist", (7,)),
            ("pcc_gc_tracing_step_cycle", (7,)),
        ]
    else:
        expected = [
            ("pcc_gc_backend4_step_remembered_roots", (7,)),
            ("pcc_gc_backend4_step_generation_aging", (7,)),
        ]
        if may_relocate:
            expected += [
                ("pcc_gc_backend4_evacuation_page_drain", (7,)),
                ("pcc_gc_backend4_select_relocation_pages", (7,)),
            ]
    if may_relocate:
        expected.append((retire, ()))
    # Unsafe records block every moving operation, not the non-moving work.
    # This executes the production dispatcher body, including both retirement
    # paths, rather than replacing its quiescence condition in the test.
    assert calls == expected



def test_host_source_pause_accumulation_max_and_full_reset_keep_high_bits():
    memory, functions = _host_functions()
    record = functions["pcc_gc_tracing_record_pause"]
    telemetry = functions["pcc_gc_telemetry"]
    memory.set("pcc_gc_metric_pause_count", (1 << 31) - 1)
    memory.set("pcc_gc_metric_pause_sum_us", (1 << 32) + 11)
    record(10, 10 + (1 << 31) + 9)
    assert telemetry(32) == 1 << 31
    assert telemetry(33) == (1 << 32) + (1 << 31) + 20
    assert telemetry(7) == (1 << 31) + 9
    record(10, 60)
    assert telemetry(7) == (1 << 31) + 9
    record(10, 10 + (1 << 32) + 23)
    assert telemetry(7) == (1 << 32) + 23
    record(10, 10)  # A valid zero-duration pause still contributes one us.
    assert telemetry(32) == (1 << 31) + 3
    assert telemetry(33) == (1 << 33) + (1 << 31) + 94
    assert [memory.get(name) for name in HISTOGRAM] == [2, 0, 0, 2]
    before = {name: memory.get(name) for name in WIDE_METRICS}
    record(0, 100)
    record(-1, 100)
    record(20, 19)
    assert {name: memory.get(name) for name in WIDE_METRICS} == before

    # Nonzero upper words make a mistaken i32 reset observable.
    for name in WIDE_METRICS:
        memory.set(name, (1 << 40) + 31)
    for name in NARROW_COUNTERS:
        memory.set(name, 37)
    functions["pcc_gc_telemetry_reset"]()
    for name, (metric, _) in WIDE_METRICS.items():
        assert memory.get(name) == telemetry(metric) == 0
    assert all(memory.get(name) == 0 for name in NARROW_COUNTERS + HISTOGRAM)
    record(10, 12)
    assert (telemetry(7), telemetry(32), telemetry(33)) == (2, 1, 2)


def test_host_source_generic_and_direct_step_writers_keep_high_bits():
    memory, functions = _host_functions()
    increment = functions["_counter_inc"]
    telemetry = functions["pcc_gc_telemetry"]
    for metric, name in enumerate(NARROW_COUNTERS):
        memory.set(name, 10 + metric)
        increment(metric, 7)
        assert memory.get(name) == telemetry(metric) == 17 + metric
    memory.set("pcc_gc_metric_step", (1 << 31) - 1)
    increment(5, 1)
    assert telemetry(5) == 1 << 31
    # _counter_global historically routes every unknown ID, including
    # negatives, to the step slot. The width branch must match that rule.
    for metric in (-1, 6, 999):
        previous = memory.get("pcc_gc_metric_step")
        increment(metric, (1 << 32) + 3)
        assert telemetry(5) == previous + (1 << 32) + 3
    increment(5, -2)
    assert telemetry(5) == (1 << 31) + 3 * (1 << 32) + 7
    assert [memory.get(name) for name in NARROW_COUNTERS] == [17, 18, 19, 20, 21]

    # Entire public functions run; work providers are intentionally idle.
    # Both direct writers execute, with no claim about collector work policy.
    for backend in range(5):
        memory.set("pcc_gc_backend_selected", backend)
        memory.set("pcc_gc_metric_step", (1 << 32) + 13)
        assert functions["pcc_gc_step"](0) == 0
        assert telemetry(5) == (1 << 32) + 13
        assert functions["pcc_gc_step"](1) == 0
        assert telemetry(5) == (1 << 32) + 14
    memory.set("pcc_gc_backend4_remap_active", 1)
    assert functions["pcc_gc_step"](1) == 0
    assert telemetry(5) == (1 << 32) + 14


def _raw_annotations(function):
    """Adapt only legacy machine-boundary annotations for the isolated probe."""
    annotations = [argument.annotation for argument in function.args.args]
    annotations.append(function.returns)
    annotations.extend(
        node.annotation for node in ast.walk(function) if isinstance(node, ast.AnnAssign)
    )
    for annotation in annotations:
        if isinstance(annotation, ast.Name) and annotation.id == "int":
            annotation.id = "i64"
    return function


def _native_slice_source():
    functions = [
        _raw_annotations(_function(BACKEND, "_counter_global")),
        _raw_annotations(_function(BACKEND, "_counter_inc")),
        _function(SCHEDULER, "pcc_gc_tracing_record_pause"),
    ]
    for helper in functions[:2]:
        assert not helper.decorator_list
        helper.decorator_list = [ast.Call(
            func=ast.Name(id="c_abi_export", ctx=ast.Load()),
            args=[ast.Constant(value="gc_phase_test" + helper.name)], keywords=[],
        )]
    telemetry = _function(TELEMETRY, "pcc_gc_telemetry")
    wanted = set(range(6)) | {7, 32, 33, 34, 35, 36, 37}
    branches = []
    for statement in telemetry.body:
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "metric"
            and len(test.ops) == len(test.comparators) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value in wanted
        ):
            branches.append(statement)
    assert len(branches) == len(wanted), "telemetry dispatch shape changed"
    # Preserve the selected production dispatch statements verbatim; omit
    # initialization and unrelated queries, whose owners are outside this test.
    telemetry.body = branches + [ast.Return(value=ast.Constant(value=-1))]
    functions.append(telemetry)

    reset = _raw_annotations(_function(BACKEND, "pcc_gc_telemetry_reset"))
    starts = [
        index for index, statement in enumerate(reset.body)
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name) and statement.target.id == "i"
    ]
    ends = [
        index for index, statement in enumerate(reset.body)
        if any(_global_name(node) == HISTOGRAM[-1] for node in ast.walk(statement))
    ]
    assert len(starts) == len(ends) == 1 and starts[0] < ends[0], "reset slice changed"
    # Keep the real loop and all counter-reset statements through hist3.
    # Remembered-set/epoch cleanup before and after this slice is not tested.
    reset.body = reset.body[starts[0]:ends[0] + 1]
    functions.append(reset)
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    return (
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import (global_addr, load_i32, load_i64, store_i32, store_i64)\n"
        "__pcc_freestanding__ = True\n\n"
        + ast.unparse(module) + "\n\n" + _NATIVE_PROBE
    )


_NATIVE_PROBE = '''@c_abi_export("gc_phase_width_probe")
def gc_phase_width_probe() -> i64:
    if pcc_gc_telemetry(5) != 0 or pcc_gc_telemetry(7) != 0:
        return 1
    if pcc_gc_telemetry(32) != 0 or pcc_gc_telemetry(33) != 0:
        return 2
    store_i64(global_addr("pcc_gc_metric_step"), 0, 2147483647)
    _counter_inc(5, 1)
    if pcc_gc_telemetry(5) != 2147483648:
        return 3
    _counter_inc(-1, 2147483648)
    _counter_inc(6, 19)
    if pcc_gc_telemetry(5) != 4294967315:
        return 4
    _counter_inc(999, -3)
    if pcc_gc_telemetry(5) != 4294967312:
        return 5
    _counter_inc(0, 31)
    _counter_inc(1, 32)
    _counter_inc(2, 33)
    _counter_inc(3, 34)
    _counter_inc(4, 35)
    if pcc_gc_telemetry(0) != 31 or pcc_gc_telemetry(1) != 32:
        return 6
    if pcc_gc_telemetry(2) != 33 or pcc_gc_telemetry(3) != 34:
        return 7
    if pcc_gc_telemetry(4) != 35 or pcc_gc_telemetry(5) != 4294967312:
        return 8
    store_i64(global_addr("pcc_gc_metric_pause_count"), 0, 2147483647)
    store_i64(global_addr("pcc_gc_metric_pause_sum_us"), 0, 4294967307)
    pcc_gc_tracing_record_pause(10, 2147483667)
    if pcc_gc_telemetry(32) != 2147483648:
        return 9
    if pcc_gc_telemetry(33) != 6442450964 or pcc_gc_telemetry(7) != 2147483657:
        return 10
    pcc_gc_tracing_record_pause(10, 60)
    if pcc_gc_telemetry(7) != 2147483657:
        return 11
    pcc_gc_tracing_record_pause(10, 4294967329)
    pcc_gc_tracing_record_pause(10, 10)
    if pcc_gc_telemetry(7) != 4294967319 or pcc_gc_telemetry(32) != 2147483651:
        return 12
    if pcc_gc_telemetry(33) != 10737418334:
        return 13
    if pcc_gc_telemetry(34) != 2 or pcc_gc_telemetry(37) != 2:
        return 14
    pcc_gc_tracing_record_pause(0, 100)
    pcc_gc_tracing_record_pause(20, 19)
    if pcc_gc_telemetry(32) != 2147483651:
        return 15
    store_i64(global_addr("pcc_gc_metric_pause_count"), 0, 1099511627807)
    pcc_gc_telemetry_reset()
    if pcc_gc_telemetry(5) != 0 or pcc_gc_telemetry(7) != 0:
        return 16
    if pcc_gc_telemetry(32) != 0 or pcc_gc_telemetry(33) != 0:
        return 17
    if pcc_gc_telemetry(0) != 0 or pcc_gc_telemetry(4) != 0:
        return 18
    if pcc_gc_telemetry(34) != 0 or pcc_gc_telemetry(37) != 0:
        return 19
    pcc_gc_tracing_record_pause(10, 12)
    if pcc_gc_telemetry(7) != 2 or pcc_gc_telemetry(32) != 1 or pcc_gc_telemetry(33) != 2:
        return 20
    return 0
'''


def _linux_x86_64():
    return platform.system() == "Linux" and platform.machine().lower() in ("x86_64", "amd64")


@pytest.mark.pcc_gate(probe=_linux_x86_64)
def test_host_pcc_self_emitted_elf_phase_counters_keep_high_bits(tmp_path, monkeypatch):
    if not _linux_x86_64():
        pytest.fail("executing this owned ELF probe requires x86-64 Linux")
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.x86_64_asm_driver import assemble_file
    from pcc.frontends.python.pipeline import compile_python
    from pcc.tools.ir_to_obj import emit_object

    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "pcc")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "gc_phase_width_probe.py"
    source.write_text(_native_slice_source(), encoding="utf-8")
    target = "x86_64-unknown-linux-gnu"
    startup = (
        ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n"
        " call gc_phase_width_probe\n mov edi, eax\n mov eax, 60\n syscall\n"
    )
    objects = []
    # No runtime archive, native bootstrap, external compiler/assembler/linker,
    # or build subprocess is allowed even accidentally through this slice.
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external build process")):
        for name, input_path in (("state", STATE), ("probe", source)):
            ir_path = tmp_path / (name + ".ll")
            compile_python(
                str(input_path), str(ir_path), emit_llvm_only=True,
                libpython_mode="off", python_library=True,
                backend="self", target_triple=target,
            )
            blob = emit_object(ir_path.read_text(encoding="utf-8"), target_triple=target)
            (tmp_path / (name + ".o")).write_bytes(blob)
            objects.append(parse_relocatable(blob))
        symbols = {symbol.name: symbol for symbol in objects[0].symbols}
        for name in WIDE_METRICS:
            assert symbols[name].size == 8
            assert symbols[name].value % 8 == 0
        for name in NARROW_COUNTERS + HISTOGRAM:
            assert symbols[name].size == 4
        ranges = sorted((symbols[name].section_index, symbols[name].value,
                         symbols[name].value + symbols[name].size, name)
                        for name in (*WIDE_METRICS, *NARROW_COUNTERS, *HISTOGRAM))
        for previous, following in zip(ranges, ranges[1:]):
            if previous[0] == following[0]:
                assert previous[2] <= following[1], ("overlapping state symbols", previous, following)
        image = link_static_executable([*objects, assemble_file(startup)])
    executable = tmp_path / "gc_phase_width_probe"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run(
        [str(executable)], capture_output=True, timeout=10, env={"PATH": ""},
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b""), (
        "isolated phase-counter probe failed at numbered check", result,
    )
