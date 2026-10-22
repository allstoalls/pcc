"""Finite host controls for emitter-local global-name indexing."""

import ast
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.backend import self_backend_precise_stackmaps as planner
from pcc.backend.native_object import encode_native_object_from_sections
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_prepare import prepare_module_for_target
from tests.python.test_precise_stackmap_abi import (
    _stale_managed_ssa_ir,
    _target_final_stackmap_ir,
)
from tests.scalar_arena_projection_ci_reference import disable_scalar_projections
from tests.stackmap_global_index_reference import (
    BASELINE_NATIVE_AST_SHA256,
    BASELINE_NATIVE_UNPARSE_SHA256,
    without_shared_globals_index,
)


def old_native_planner():
    source = Path(planner.__file__).read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_build_function_stack_map_plan_native")
    function = without_shared_globals_index(function)
    assert (
        hashlib.sha256(ast.dump(function).encode()).hexdigest() == BASELINE_NATIVE_AST_SHA256
        or hashlib.sha256(ast.unparse(function).encode()).hexdigest()
        == BASELINE_NATIVE_UNPARSE_SHA256
    )
    namespace = dict(vars(planner))
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<2d8c889-native-planner>", "exec"), namespace)
    return namespace[function.name]


def source_text(shape="derived", triple="arm64-apple-darwin", functions=3):
    if shape == "tail-call":
        header = ('target triple = "' + triple + '"\n'
                  '@unused = internal constant i32 1\n'
                  'declare i64 @callee(i64)\n')
        return header + "\n".join('''define i64 @tail%d(i64 %%value) {
entry:
  %%result = call i64 @callee(i64 %%value)
  ret i64 %%result
}''' % index for index in range(functions))
    factory = _stale_managed_ssa_ir if shape == "derived" else _target_final_stackmap_ir
    source = factory(triple)
    start = source.index("define ")
    body = source[start:]
    name = "refresh" if shape == "derived" else "probe"
    return source[:start] + "\n".join(
        body.replace("@" + name + "(", "@" + name + str(index) + "(")
        for index in range(functions)
    )


def prepared_module(source=None):
    return prepare_module_for_target(
        source or source_text(), aggregate_returned_indirect=lambda _ty: False,
    )


def plan_values(plans):
    return tuple((plan.function_name, plan.function_id, plan.frame_size, plan.end_label,
                  plan.diagnostic_records(), plan.block_entry_labels,
                  plan.instruction_suffix_labels, plan.terminator_prefix_labels,
                  plan.target) for plan in plans)


def test_exact_2d8c889_native_algorithm_is_the_control():
    assert old_native_planner().__name__ == "_build_function_stack_map_plan_native"


@pytest.mark.parametrize("shape", ("derived", "all-record-kinds"))
@pytest.mark.parametrize("functions", (1, 7))
def test_full_plans_match_original_algorithm(monkeypatch, shape, functions):
    source = source_text(shape, functions=functions)
    prepared = prepared_module(source)
    old = old_native_planner()
    expected = plan_values(tuple(old(func, prepared.globals_, "_" + func.name)
                                 for func in prepared.functions))
    actual = planner.build_stack_map_plans(
        prepared.functions, prepared.globals_, target="aarch64-darwin",
        function_symbol=lambda name: "_" + name, _share_globals_index=True,
    )
    assert plan_values(actual) == expected


@pytest.mark.parametrize(("target", "opt_in"), (
    ("aarch64-darwin", False), ("x86_64-linux", False), ("x86_64-linux", True),
))
def test_public_callback_mutations_keep_per_function_behavior(target, opt_in):
    triple = "arm64-apple-darwin" if target == "aarch64-darwin" else "x86_64-linux-gnu"
    prepared = prepared_module(source_text(triple=triple, functions=2))
    original = prepared.globals_[0]
    calls = []

    def symbol(name):
        calls.append(name)
        if len(calls) == 2:
            prepared.globals_[0] = replace(original, initializer="65536")
        return name

    with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
        planner.build_stack_map_plans(prepared.functions, prepared.globals_, target=target,
            function_symbol=symbol, _share_globals_index=opt_in)
    assert len(calls) == 2


@pytest.mark.parametrize("share", (False, True))
@pytest.mark.parametrize("last_valid", (False, True))
def test_duplicate_names_still_use_last_global(share, last_valid):
    prepared = prepared_module()
    valid = prepared.globals_[0]
    invalid = replace(valid, initializer="65536")
    globals_ = [invalid, valid] if last_valid else [valid, invalid]
    if last_valid:
        plans = planner.build_stack_map_plans(prepared.functions, globals_,
            target="aarch64-darwin", _share_globals_index=share)
        assert len(plans) == len(prepared.functions)
    else:
        with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
            planner.build_stack_map_plans(prepared.functions, globals_,
                target="aarch64-darwin", _share_globals_index=share)


@pytest.mark.parametrize("share", (False, True))
def test_identity_kernel_index_order_and_empty_input(monkeypatch, share):
    prepared = prepared_module()
    events = []

    class Globals(list):
        def __iter__(self):
            events.append("globals")
            return super().__iter__()

    globals_ = Globals(prepared.globals_)
    kernel = planner.get_indexed_function_kernel

    def lookup(func):
        events.append("kernel:" + func.name)
        return kernel(func)

    def identity(name):
        events.append("identity:" + name)
        return name

    monkeypatch.setattr(planner, "get_indexed_function_kernel", lookup)
    assert planner.build_stack_map_plans([], globals_, target="unsupported",
        function_symbol=identity, _share_globals_index=share) == ()
    assert not events
    planner.build_stack_map_plans(prepared.functions, globals_, target="aarch64-darwin",
        function_symbol=identity, _share_globals_index=share)
    expected = []
    for index, func in enumerate(prepared.functions):
        expected += ["identity:" + func.name, "kernel:" + func.name]
        if index == 0 or not share:
            expected.append("globals")
    assert events == expected


def test_kernel_failure_precedes_index_construction(monkeypatch):
    prepared = prepared_module()

    class Globals(list):
        def __iter__(self):
            raise AssertionError("must not enumerate globals after kernel failure")

    def fail(func):
        raise ValueError("kernel failure")

    monkeypatch.setattr(planner, "get_indexed_function_kernel", fail)
    with pytest.raises(ValueError, match="kernel failure"):
        planner.build_stack_map_plans(prepared.functions, Globals(prepared.globals_),
            target="aarch64-darwin", _share_globals_index=True)


@pytest.mark.parametrize("failure", (None, "first", "later", "identity"))
def test_batch_table_cleared_on_success_and_saved_exception(monkeypatch, failure):
    prepared = prepared_module()
    root_states = planner._native_root_states
    seen, snapshots, saved = [], [], []

    def roots(func, index, aliases, kernel):
        seen.append(index)
        snapshots.append(tuple(index.items()))
        if failure == "first" or (failure == "later" and len(seen) == 2):
            raise ValueError("planning failure")
        return root_states(func, index, aliases, kernel)

    def identity(name):
        if failure == "identity" and seen:
            raise ValueError("identity failure")
        return name

    monkeypatch.setattr(planner, "_native_root_states", roots)
    try:
        plans = planner.build_stack_map_plans(prepared.functions, prepared.globals_,
            target="aarch64-darwin", function_symbol=identity, _share_globals_index=True)
        assert failure is None and len(plans) == 3
    except ValueError as error:
        assert failure is not None
        saved.append(error)  # Keep every traceback frame alive through the checks.
    assert seen and all(index is seen[0] for index in seen)
    assert snapshots and all(snapshot == snapshots[0] for snapshot in snapshots)
    assert all(not index for index in seen)
    assert prepared.globals_ and prepared.globals_[0].name == "frame_map"


def test_empty_globals_initialized_once_and_no_index_survives_next_batch(monkeypatch):
    prepared = prepared_module('''target triple = "arm64-apple-darwin"
define void @a() {
entry:
  ret void
}
define void @b() {
entry:
  ret void
}
''')
    iterations = []

    class Globals(list):
        def __iter__(self):
            iterations.append(len(self))
            return super().__iter__()

    globals_ = Globals()
    for _ in range(2):
        planner.build_stack_map_plans(prepared.functions, globals_,
            target="aarch64-darwin", _share_globals_index=True)
    assert iterations == [0, 0]
    prepared = prepared_module()
    planner.build_stack_map_plans(prepared.functions, prepared.globals_,
        target="aarch64-darwin", _share_globals_index=True)
    prepared.globals_[0] = replace(prepared.globals_[0], initializer="65536")
    with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
        planner.build_stack_map_plans(prepared.functions, prepared.globals_,
            target="aarch64-darwin", _share_globals_index=True)


@pytest.mark.parametrize("shape", ("derived", "all-record-kinds", "tail-call"))
@pytest.mark.parametrize("triple", ("arm64-apple-darwin", "aarch64-unknown-linux-gnu"))
@pytest.mark.parametrize("optimize", (False, True))
@pytest.mark.parametrize("route", ("assembly", "structured-pco"))
@pytest.mark.parametrize("reference", ("global-index", "scratch-retirement"))
def test_complete_output_matches_original_planner(monkeypatch, shape, triple, optimize, route, reference):
    source = source_text(shape, triple)
    outputs = []
    original_batch = planner.build_stack_map_plans

    def old_batch(functions, globals_, *, target, function_symbol=None, **kwargs):
        return original_batch(functions, globals_, target=target, function_symbol=function_symbol)

    for original in (True, False):
        replanned = []
        public_plan = emitter.build_function_stack_map_plan

        def replan(func, *args, **kwargs):
            replanned.append(func.name)
            return public_plan(func, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(emitter, "build_function_stack_map_plan", replan)
            if original:
                if reference == "global-index":
                    patch.setattr(planner, "_build_function_stack_map_plan_native", old_native_planner())
                    patch.setattr(emitter, "build_stack_map_plans", old_batch)
                else:
                    # Exercise the real shared-index batch entry with the
                    # frozen pre-retirement planner, as the CI reference does.
                    disable_scalar_projections(patch)
            if route == "assembly":
                result = emitter.emit_aarch64_darwin_asm(source, optimize=optimize)
            else:
                transport = emitter.emit_aarch64_darwin_indexed_transport(
                    parse_self_backend_module(source), optimize=optimize,
                    structured_instructions=True,
                )
                sections, undefined = transport.assemble_sections()
                result = encode_native_object_from_sections(sections, undefined=undefined)
            outputs.append(result)
        assert replanned == (["tail0", "tail1", "tail2"]
                             if shape == "tail-call" and optimize else [])
    assert outputs[0] == outputs[1]


def test_emitter_releases_batch_index_before_tailcall_replanning(monkeypatch):
    native = planner._build_function_stack_map_plan_native
    holders, replanned = [], []

    def observe(func, globals_, identity, _globals_index=None):
        if _globals_index is None:
            assert holders and all(not holder for holder in holders)
            replanned.append(func.name)
        else:
            holders.append(_globals_index)
        return native(func, globals_, identity, _globals_index)

    monkeypatch.setattr(planner, "_build_function_stack_map_plan_native", observe)
    emitter.emit_aarch64_darwin_asm(source_text("tail-call"), optimize=True)
    assert len(holders) == 3 and all(holder is holders[0] for holder in holders)
    assert not holders[0] and replanned == ["tail0", "tail1", "tail2"]


@pytest.mark.parametrize("shape", ("derived", "all-record-kinds", "tail-call"))
@pytest.mark.parametrize("holder_kind", ("omitted", "empty", "populated"))
def test_snapshot_reference_keeps_per_function_indexing(monkeypatch, shape, holder_kind):
    prepared = prepared_module(source_text(shape, functions=3))
    expected = plan_values(planner.build_stack_map_plans(
        prepared.functions, prepared.globals_, target="aarch64-darwin",
        function_symbol=lambda name: "_" + name,
    ))
    iterations = []

    class Globals(list):
        def __iter__(self):
            iterations.append(len(self))
            return super().__iter__()

    globals_ = Globals(prepared.globals_)
    sentinel = {"must-not-be-used": object()}
    holder = [sentinel] if holder_kind == "populated" else []
    native = planner._build_function_stack_map_plan_native
    liveness = planner._native_managed_liveness
    projection = planner._scalar_window_is_complete
    with monkeypatch.context() as patch:
        disable_scalar_projections(patch)
        reference = planner._build_function_stack_map_plan_native
        assert reference is not native
        plans = []
        for func in prepared.functions:
            args = (func, globals_, "_" + func.name)
            if holder_kind != "omitted":
                args += (holder,)
            plans.append(reference(*args))
        assert plan_values(plans) == expected
        assert iterations == [len(globals_)] * len(prepared.functions)
        if holder_kind == "populated":
            assert len(holder) == 1 and holder[0] is sentinel
            assert list(sentinel) == ["must-not-be-used"]
        else:
            assert holder == []
    assert planner._build_function_stack_map_plan_native is native
    assert planner._native_managed_liveness is liveness
    assert planner._scalar_window_is_complete is projection


@pytest.mark.parametrize("share", (False, True))
def test_snapshot_reference_batch_preserves_per_function_global_mutations(monkeypatch, share):
    prepared = prepared_module(source_text(functions=2))
    original = prepared.globals_[0]
    calls = []

    def symbol(name):
        calls.append(name)
        if len(calls) == 2:
            prepared.globals_[0] = replace(original, initializer="65536")
        return name

    with monkeypatch.context() as patch:
        disable_scalar_projections(patch)
        with pytest.raises(BackendUnavailable, match="exceeds the ABI location bound"):
            planner.build_stack_map_plans(
                prepared.functions, prepared.globals_, target="aarch64-darwin",
                function_symbol=symbol, _share_globals_index=share,
            )
    assert len(calls) == 2
